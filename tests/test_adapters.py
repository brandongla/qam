"""Adapter unit tests.

Message samples follow the formats used by the official SDKs (hyperliquid-python-sdk
websocket_manager, lighter-python ws_client). They must be re-validated against real captures
from the VM smoke test before normalizers rely on field details.
"""

from __future__ import annotations

import httpx
import orjson
import pytest

from qam.recorders.config import load_config
from qam.recorders.venues import HyperliquidAdapter, LighterAdapter
from qam.recorders.venues.base import Market


def _markets() -> list[Market]:
    return [
        Market("BTC", "BTC", "perp", True, 1e9),
        Market("ETH", "ETH", "perp", True, 5e8),
        Market("SOL", "SOL", "perp", True, 2e8),
        Market("DOGE", "DOGE", "perp", True, 1e8),
        Market("TINY", "TINY", "perp", True, 1e3),
        Market("DEAD", "DEAD", "perp", False, 0.0),
    ]


@pytest.fixture
def hl() -> HyperliquidAdapter:
    cfg = load_config(overrides={"venues": {"hyperliquid": {"l2": {"top_n_by_volume": 1}}}})
    return HyperliquidAdapter(cfg.venues["hyperliquid"])


@pytest.fixture
def lt() -> LighterAdapter:
    cfg = load_config(overrides={"venues": {"lighter": {"l2": {"markets": ["BTC"], "top_n_by_volume": 1}}}})
    return LighterAdapter(cfg.venues["lighter"])


# -- Hyperliquid ------------------------------------------------------------------------------


def test_hl_plan_contents_and_priority(hl: HyperliquidAdapter) -> None:
    subs = hl.plan(_markets())
    keys = [s.key for s in subs]
    assert len(keys) == len(set(keys))
    assert not any("DEAD" in k for k in keys)  # delisted markets excluded
    # L2 set = explicit BTC/ETH/SOL + top-1 remaining by volume (DOGE)
    assert {k for k in keys if k.startswith("l2Book:") and ":sf" not in k} == {
        f"l2Book:{c}" for c in ("BTC", "ETH", "SOL", "DOGE")
    }
    # Coarser aggregations are isolated in their own connection groups and streams.
    sf = [s for s in subs if ":sf" in s.key]
    assert sf and all(s.group == s.stream and s.classify_as == "l2" for s in sf)
    # Per-market streams are ordered by volume, so a cap drops the smallest markets first.
    trade_order = [s.key for s in subs if s.key.startswith("trades:")]
    assert trade_order == ["trades:BTC", "trades:ETH", "trades:SOL", "trades:DOGE", "trades:TINY"]
    assert keys.index("l2Book:BTC") < keys.index("trades:BTC")
    msg = orjson.loads(next(s.message for s in subs if s.key == "bbo:ETH"))
    assert msg == {"method": "subscribe", "subscription": {"type": "bbo", "coin": "ETH"}}


@pytest.mark.parametrize(
    ("text", "stream", "market", "is_data"),
    [
        ('{"channel":"trades","data":[{"coin":"BTC","px":"1","sz":"2","time":1}]}', "trades", "BTC", True),
        ('{"channel":"l2Book","data":{"coin":"ETH","time":1,"levels":[[],[]]}}', "l2", "ETH", True),
        ('{"channel":"bbo","data":{"coin":"SOL","time":1,"bbo":[null,null]}}', "bbo", "SOL", True),
        (
            '{"channel":"activeAssetCtx","data":{"coin":"BTC","ctx":{"funding":"0.0001"}}}',
            "asset_ctx",
            "BTC",
            True,
        ),
        ('{"channel":"trades","data":[]}', "trades", None, True),
        ('{"channel":"pong"}', "control", None, False),
        ('{"channel":"subscriptionResponse","data":{"method":"subscribe"}}', "control", None, False),
        ("Websocket connection established.", "control", None, False),
        ('{"channel":"somethingNew","data":{}}', "unknown", None, True),
    ],
)
def test_hl_classify(
    hl: HyperliquidAdapter, text: str, stream: str, market: str | None, is_data: bool
) -> None:
    c = hl.classify(text)
    assert (c.stream, c.market, c.is_data) == (stream, market, is_data)


def test_hl_error_and_keepalive(hl: HyperliquidAdapter) -> None:
    c = hl.classify('{"channel":"error","data":"Invalid subscription"}')
    assert c.error == "Invalid subscription" and not c.is_data
    assert orjson.loads(hl.keepalive_message() or "") == {"method": "ping"}


async def test_hl_discover_parses_meta_and_ctx(hl: HyperliquidAdapter) -> None:
    payload = [
        {"universe": [{"name": "BTC", "szDecimals": 5}, {"name": "OLD", "isDelisted": True}]},
        [{"dayNtlVlm": "123.5"}, {"dayNtlVlm": "0"}],
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/info" and orjson.loads(req.content) == {"type": "metaAndAssetCtxs"}
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await hl.discover(client)
    assert [(m.symbol, m.active, m.volume_24h_usd) for m in res.markets] == [
        ("BTC", True, 123.5),
        ("OLD", False, 0.0),
    ]
    assert res.raw_responses and res.raw_responses[0][0].endswith("/info")


# -- Lighter ----------------------------------------------------------------------------------


def _lt_markets() -> list[Market]:
    return [
        Market("BTC", "1", "perp", True, 5e8),
        Market("ETH", "0", "perp", True, 9e8),
        Market("XYZ", "7", "perp", True, 1e6),
        Market("OFF", "9", "perp", False, 0.0),
    ]


def test_lt_plan(lt: LighterAdapter) -> None:
    subs = lt.plan(_lt_markets())
    keys = [s.key for s in subs]
    assert keys[0] == "market_stats/all"
    # L2 = explicit BTC + top-1 remaining (ETH); ordered by volume
    assert [k for k in keys if k.startswith("order_book/")] == ["order_book/0", "order_book/1"]
    assert [k for k in keys if k.startswith("trade/")] == ["trade/0", "trade/1", "trade/7"]
    assert orjson.loads(subs[0].message) == {"type": "subscribe", "channel": "market_stats/all"}
    assert lt.subscribe_on_open is False


def test_lt_classify(lt: LighterAdapter) -> None:
    lt.plan(_lt_markets())  # learns id -> symbol
    c = lt.classify('{"type":"connected"}')
    assert c.ready and not c.is_data
    c = lt.classify('{"type":"ping"}')
    assert orjson.loads(c.reply or "") == {"type": "pong"} and not c.is_data
    c = lt.classify(
        '{"channel":"order_book:1","offset":42,"order_book":{"asks":[],"bids":[]},"type":"update/order_book"}'
    )
    assert (c.stream, c.market, c.seq) == ("order_book", "BTC", ("order_book:1", 42))
    c = lt.classify('{"channel":"trade:0","trades":[],"type":"update/trade"}')
    assert (c.stream, c.market) == ("trades", "ETH")
    c = lt.classify('{"channel":"market_stats:all","market_stats":{},"type":"update/market_stats"}')
    assert (c.stream, c.market) == ("market_stats", "all")
    c = lt.classify('{"type":"shutdown","close_in_ms":1000}')
    assert c.channel == "shutdown" and not c.is_data
    c = lt.classify('{"error":{"code":30003,"message":"Invalid Channel"}}')
    assert c.error and c.stream == "control"
    assert lt.classify('{"channel":"new_thing:1","type":"update/new_thing"}').stream == "unknown"


async def test_lt_discover_with_fallback(lt: LighterAdapter) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/orderBookDetails":
            return httpx.Response(404)
        assert req.url.path == "/api/v1/orderBooks"
        return httpx.Response(
            200,
            json={"code": 200, "order_books": [{"symbol": "ETH", "market_id": 0, "status": "active"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await lt.discover(client)
    assert [(m.symbol, m.venue_id, m.volume_24h_usd) for m in res.markets] == [("ETH", "0", None)]


async def test_lt_discover_details(lt: LighterAdapter) -> None:
    body = {
        "code": 200,
        "order_book_details": [
            {"symbol": "ETH", "market_id": 0, "status": "active", "daily_quote_token_volume": 1.5e9},
            {"symbol": "OLD", "market_id": 5, "status": "inactive", "daily_quote_token_volume": 0},
        ],
        "spot_order_book_details": [{"symbol": "ETH/USDC", "market_id": 2048, "status": "active"}],
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    ) as c:
        res = await lt.discover(c)
    assert [(m.symbol, m.active) for m in res.markets] == [
        ("ETH", True),
        ("OLD", False),
    ]  # spot off by default
