"""End-to-end: recorder <-> local fake venue servers -> raw archive."""

from __future__ import annotations

import asyncio
import functools
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import orjson

from qam.data.raw import iter_records, verify_archive
from qam.recorders.config import load_config
from qam.recorders.runner import run_recorders, shard_subscriptions
from qam.recorders.venues.base import Subscription

from .fakes import FakeServer, hyperliquid_like, lighter_like, silent

FAST_CONN = {
    "backoff_initial_s": 0.05,
    "backoff_max_s": 0.2,
    "subscribe_rate_per_s": 1000,
    "open_timeout_s": 3,
}


def _hl_rest(coins: list[str]) -> Any:
    payload = [
        {"universe": [{"name": c} for c in coins]},
        [{"dayNtlVlm": str(1000 - i)} for i in range(len(coins))],
    ]
    return lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    )


def _lt_rest() -> Any:
    body = {
        "code": 200,
        "order_book_details": [
            {"symbol": "ETH", "market_id": 0, "status": "active", "daily_quote_token_volume": 2e9},
            {"symbol": "BTC", "market_id": 1, "status": "active", "daily_quote_token_volume": 1e9},
        ],
    }
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))


def _cfg(tmp_path: Path, venue: str, url: str, **conn: Any) -> Any:
    return load_config(
        overrides={
            "data_root": str(tmp_path),
            "health_interval_s": 0.2,
            "archive": {"flush_interval_s": 0.2},
            "venues": {venue: {"ws_url": url, "connection": {**FAST_CONN, **conn}}},
        }
    )


def _meta(tmp_path: Path, venue: str) -> list[dict[str, Any]]:
    return list(iter_records(tmp_path, venue, "meta"))


async def test_hyperliquid_records_and_resubscribes_after_drop(tmp_path: Path) -> None:
    handler = functools.partial(hyperliquid_like, drop_first_after=2)
    async with FakeServer(handler) as srv:
        cfg = _cfg(tmp_path, "hyperliquid", srv.url, max_subscriptions=1000)
        healths = await run_recorders(
            cfg, ["hyperliquid"], duration_s=1.5, http_client=_hl_rest(["BTC", "ETH", "SOL"])
        )
        assert srv.connections >= 2  # server dropped connection 1 -> recorder reconnected

    h = healths["hyperliquid"]
    assert h.reconnects >= 1 and h.venue_errors == 0
    # The dropped main connection was replaced by one that re-sent every main-group subscription.
    dropped = srv.state["dropped"]
    by_conn: dict[int, list[dict[str, Any]]] = {}
    for n, m in srv.received:
        if "subscription" in m:
            by_conn.setdefault(n, []).append(m["subscription"])
    main_subs = [s for s in by_conn[dropped]]
    replacement = [
        n for n, subs in by_conn.items() if n > dropped and any(x["type"] == "trades" for x in subs)
    ]
    assert replacement, "no replacement main connection"
    n_main = sum(1 for s in by_conn[replacement[0]] if "nSigFigs" not in s)
    assert n_main == h.subscriptions_planned - 6  # 3 coins x 2 extra aggregations live elsewhere
    assert len(main_subs) < n_main

    trades = list(iter_records(tmp_path, "hyperliquid", "trades"))
    assert {r["mkt"] for r in trades} == {"BTC", "ETH", "SOL"}
    assert all(r["k"] == "msg" and r["raw"].startswith('{"channel":"trades"') for r in trades)
    assert list(iter_records(tmp_path, "hyperliquid", "bbo"))
    # Coarser aggregations went to separate streams via their own connection group.
    assert list(iter_records(tmp_path, "hyperliquid", "l2_sf4")) == []  # fake doesn't send l2Book
    events = Counter(r["event"] for r in _meta(tmp_path, "hyperliquid"))
    assert events["connected"] >= 2 and events["disconnected"] >= 1 and events["subscribed"] >= 2
    assert events["plan"] == 1 and events["recorder_start"] == 1
    # Market metadata response archived as reference data.
    ref = list(iter_records(tmp_path, "hyperliquid", "reference"))
    assert ref and ref[0]["ch"].endswith("/info")
    assert verify_archive(tmp_path, deep=True).passed
    assert not list(tmp_path.rglob("*.part"))
    health = orjson.loads((tmp_path / "health" / "hyperliquid.json").read_bytes())
    assert health["streams"]["trades"]["records"] == len(trades)


async def test_hyperliquid_keepalive_ping_sent(tmp_path: Path, monkeypatch: Any) -> None:
    from qam.recorders.venues.hyperliquid import HyperliquidAdapter

    monkeypatch.setattr(HyperliquidAdapter, "keepalive_interval_s", 0.1)
    async with FakeServer(hyperliquid_like) as srv:
        cfg = _cfg(tmp_path, "hyperliquid", srv.url, max_subscriptions=1000)
        await run_recorders(cfg, ["hyperliquid"], duration_s=0.8, http_client=_hl_rest(["BTC"]))
    assert any(m.get("method") == "ping" for _, m in srv.received)
    control = list(iter_records(tmp_path, "hyperliquid", "control"))
    assert any(r["ch"] == "pong" for r in control)


async def test_lighter_waits_for_connected_answers_ping_and_flags_offset_regression(tmp_path: Path) -> None:
    async with FakeServer(lighter_like) as srv:
        cfg = _cfg(tmp_path, "lighter", srv.url)
        healths = await run_recorders(cfg, ["lighter"], duration_s=1.0, http_client=_lt_rest())
    assert any(m == {"type": "pong"} for _, m in srv.received)
    subs = [m["channel"] for _, m in srv.received if m.get("type") == "subscribe"]
    assert "market_stats/all" in subs and "order_book/0" in subs and "trade/1" in subs
    books = list(iter_records(tmp_path, "lighter", "order_book"))
    assert {r["mkt"] for r in books} == {"ETH", "BTC"}
    assert healths["lighter"].seq_regressions >= 1
    events = [r["event"] for r in _meta(tmp_path, "lighter")]
    assert "seq_regression" in events
    sub_meta = next(r for r in _meta(tmp_path, "lighter") if r["event"] == "subscribing")
    assert sub_meta["info"]["note"] is None  # subscribed on the ready signal, not the timeout


async def test_stall_triggers_reconnect(tmp_path: Path) -> None:
    async with FakeServer(silent) as srv:
        cfg = _cfg(tmp_path, "hyperliquid", srv.url, stall_timeout_s=0.3)
        healths = await run_recorders(cfg, ["hyperliquid"], duration_s=1.2, http_client=_hl_rest(["BTC"]))
        assert srv.connections >= 2
    h = healths["hyperliquid"]
    assert h.reconnect_reasons.get("stall", 0) >= 1


async def test_discovery_failure_retries_without_crashing(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    cfg = _cfg(tmp_path, "hyperliquid", "ws://127.0.0.1:9")
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.5, stop.set)
    await run_recorders(
        cfg,
        ["hyperliquid"],
        stop,
        http_client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert calls["n"] >= 1
    events = [r["event"] for r in _meta(tmp_path, "hyperliquid")]
    assert "discovery_failed" in events


def test_shard_subscriptions_respects_groups_and_size() -> None:
    subs = [Subscription(f"k{i}", "trades", "{}") for i in range(5)] + [
        Subscription("x", "l2_sf4", "{}", group="l2_sf4", classify_as="l2")
    ]
    shards = shard_subscriptions(subs, 2)
    assert {k: len(v) for k, v in shards.items()} == {"main-0": 2, "main-1": 2, "main-2": 1, "l2_sf4-0": 1}


def test_venue_lock_is_exclusive(tmp_path: Path) -> None:
    import pytest

    from qam.recorders.runner import VenueLock

    a = VenueLock(tmp_path, "lighter")
    with pytest.raises(RuntimeError, match="already running"):
        VenueLock(tmp_path, "lighter")
    VenueLock(tmp_path, "hyperliquid").release()  # other venues are independent
    a.release()
    VenueLock(tmp_path, "lighter").release()  # free again after release
