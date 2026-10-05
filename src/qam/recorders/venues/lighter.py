"""Lighter perps adapter.

Protocol (per the official lighter-python ws_client and public examples):
- WS ``wss://mainnet.zklighter.elliot.ai/stream``. The server sends ``{"type": "connected"}``,
  then the client subscribes with ``{"type": "subscribe", "channel": "<name>/<id>"}``.
- Data messages carry ``type`` ``subscribed/<name>`` (initial snapshot) or ``update/<name>``,
  and ``channel`` ``<name>:<id>``. ``order_book`` sends a full snapshot on subscribe, then state
  changes, with an ``offset`` that increases but isn't guaranteed contiguous (and resets on
  reconnect to a different server).
- Keepalive: the server sends ``{"type": "ping"}``, and the client must answer ``{"type": "pong"}``.
  ``{"type": "shutdown"}`` announces a server-side close.
- Market discovery: ``GET /api/v1/orderBookDetails`` (includes daily volume), with fallback
  ``GET /api/v1/orderBooks``.
"""

from __future__ import annotations

from typing import Any

import httpx
import orjson

from qam.recorders.venues.base import Classified, DiscoveryResult, Market, Subscription, VenueAdapter

_STREAMS = {"order_book": "order_book", "trade": "trades", "market_stats": "market_stats"}
_PONG = orjson.dumps({"type": "pong"}).decode()


def _dumps(obj: Any) -> str:
    return orjson.dumps(obj).decode()


class LighterAdapter(VenueAdapter):
    name = "lighter"
    subscribe_on_open = False  # wait for {"type": "connected"} (runner falls back after a timeout)
    keepalive_interval_s = None  # server-initiated ping/pong

    def __init__(self, cfg) -> None:  # type: ignore[no-untyped-def]
        super().__init__(cfg)
        self._id_to_symbol: dict[str, str] = {}

    async def discover(self, client: httpx.AsyncClient) -> DiscoveryResult:
        base = self.cfg.rest_url.rstrip("/")
        raws: list[tuple[str, str | None, str]] = []
        markets: list[Market] = []
        url = base + "/api/v1/orderBookDetails"
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            raws.append((url, None, resp.text))
            body = resp.json()
            for d in body.get("order_book_details", []) or []:
                markets.append(self._market(d, "perp", d.get("daily_quote_token_volume")))
            if "spot" in self.cfg.market_types:
                for d in body.get("spot_order_book_details", []) or []:
                    markets.append(self._market(d, "spot", d.get("daily_quote_token_volume")))
        except (httpx.HTTPError, ValueError):
            url = base + "/api/v1/orderBooks"
            resp = await client.get(url)
            resp.raise_for_status()
            raws.append((url, None, resp.text))
            for d in resp.json().get("order_books", []) or []:
                mtype = "spot" if str(d.get("market_type", "perp")).lower() == "spot" else "perp"
                markets.append(self._market(d, mtype, None))
        self._id_to_symbol = {m.venue_id: m.symbol for m in markets}
        return DiscoveryResult(markets=markets, raw_responses=raws)

    @staticmethod
    def _market(d: dict[str, Any], mtype: str, vol: Any) -> Market:
        return Market(
            symbol=str(d["symbol"]),
            venue_id=str(d["market_id"]),
            market_type=mtype,
            active=str(d.get("status", "active")).lower() == "active",
            volume_24h_usd=float(vol) if vol is not None else None,
        )

    def plan(self, markets: list[Market]) -> list[Subscription]:
        """Priority order: market stats, order books (L2 set), then trades by descending volume."""
        ch = self.cfg.channels
        self._id_to_symbol = {m.venue_id: m.symbol for m in markets}
        active = [m for m in markets if m.active and m.market_type in self.cfg.market_types]
        ranked = sorted(active, key=lambda m: m.volume_24h_usd or 0.0, reverse=True)
        l2_set = self.select_l2_markets(active, self.cfg.l2.markets, self.cfg.l2.top_n_by_volume)
        l2_symbols = {m.symbol for m in l2_set}
        subs: list[Subscription] = []

        def wanted(switch: str | None, m: Market) -> bool:
            return switch == "all" or (switch == "l2" and m.symbol in l2_symbols)

        if ch.get("market_stats") == "all":
            subs.append(self._sub("market_stats", "all"))
        else:
            subs += [
                self._sub("market_stats", m.venue_id) for m in ranked if wanted(ch.get("market_stats"), m)
            ]
        subs += [self._sub("order_book", m.venue_id) for m in ranked if wanted(ch.get("order_book"), m)]
        subs += [self._sub("trade", m.venue_id) for m in ranked if wanted(ch.get("trades"), m)]
        return subs

    def _sub(self, name: str, ident: str) -> Subscription:
        return Subscription(
            key=f"{name}/{ident}",
            stream=_STREAMS[name],
            message=_dumps({"type": "subscribe", "channel": f"{name}/{ident}"}),
        )

    def classify(self, text: str) -> Classified:
        try:
            msg = orjson.loads(text)
        except orjson.JSONDecodeError:
            return Classified(stream="unknown", channel="text")
        if not isinstance(msg, dict):
            return Classified(stream="unknown")
        mtype = str(msg.get("type", ""))
        if mtype == "connected":
            return Classified(stream="control", channel=mtype, is_data=False, ready=True)
        if mtype == "ping":
            return Classified(stream="control", channel=mtype, is_data=False, reply=_PONG)
        if mtype in ("pong", "shutdown"):
            return Classified(stream="control", channel=mtype, is_data=False)
        if mtype == "error" or ("error" in msg and not mtype.startswith(("subscribed/", "update/"))):
            return Classified(
                stream="control", channel="error", is_data=False, error=str(msg.get("error", msg))
            )
        channel = msg.get("channel")
        if isinstance(channel, str) and ":" in channel:
            base, _, ident = channel.partition(":")
        else:
            base, ident = mtype.partition("/")[2], ""
        stream = _STREAMS.get(base)
        if stream is None:
            return Classified(stream="unknown", channel=channel if isinstance(channel, str) else mtype)
        market = self._id_to_symbol.get(ident, ident or None)
        seq = None
        offset = msg.get("offset")
        if base == "order_book" and isinstance(offset, int):
            seq = (f"{base}:{ident}", offset)
        return Classified(
            stream=stream, channel=channel if isinstance(channel, str) else mtype, market=market, seq=seq
        )
