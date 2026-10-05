"""Hyperliquid perps adapter.

Protocol (per the official hyperliquid-python-sdk websocket_manager):
- WS ``wss://api.hyperliquid.xyz/ws``; subscribe with
  ``{"method": "subscribe", "subscription": {"type": <t>, "coin": <c>, ...}}``.
- Data messages are ``{"channel": <t>, "data": ...}``. Control: ``subscriptionResponse``,
  ``pong``, ``error``. The server sends a plain-text greeting on connect.
- Application keepalive: send ``{"method": "ping"}`` periodically (SDK uses 50 s).
- Market discovery: ``POST /info {"type": "metaAndAssetCtxs"}``.

Note: ``l2Book`` messages don't say which ``nSigFigs`` aggregation they belong to, so each
aggregation variant gets its own connection group and archive stream (``l2``, ``l2_sf4``, ...).
"""

from __future__ import annotations

from typing import Any

import httpx
import orjson

from qam.recorders.venues.base import Classified, DiscoveryResult, Market, Subscription, VenueAdapter

_DATA_STREAMS = {
    "trades": "trades",
    "bbo": "bbo",
    "l2Book": "l2",
    "activeAssetCtx": "asset_ctx",
    "activeSpotAssetCtx": "asset_ctx",
}


def _dumps(obj: Any) -> str:
    return orjson.dumps(obj).decode()


class HyperliquidAdapter(VenueAdapter):
    name = "hyperliquid"
    subscribe_on_open = True
    keepalive_interval_s = 50.0

    async def discover(self, client: httpx.AsyncClient) -> DiscoveryResult:
        body = _dumps({"type": "metaAndAssetCtxs"})
        url = self.cfg.rest_url.rstrip("/") + "/info"
        resp = await client.post(url, content=body, headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        meta, ctxs = resp.json()
        markets: list[Market] = []
        for i, asset in enumerate(meta.get("universe", [])):
            ctx = ctxs[i] if i < len(ctxs) else {}
            vol = ctx.get("dayNtlVlm")
            markets.append(
                Market(
                    symbol=asset["name"],
                    venue_id=asset["name"],
                    market_type="perp",
                    active=not asset.get("isDelisted", False),
                    volume_24h_usd=float(vol) if vol is not None else None,
                )
            )
        return DiscoveryResult(markets=markets, raw_responses=[(url, body, resp.text)])

    def plan(self, markets: list[Market]) -> list[Subscription]:
        """Priority order: L2 books, then per-market streams by descending volume.

        If the venue-wide cap truncates the plan, the lowest-volume markets lose coverage first.
        """
        ch = self.cfg.channels
        active = [m for m in markets if m.active and m.market_type in self.cfg.market_types]
        ranked = sorted(active, key=lambda m: m.volume_24h_usd or 0.0, reverse=True)
        l2_set = self.select_l2_markets(active, self.cfg.l2.markets, self.cfg.l2.top_n_by_volume)
        l2_symbols = {m.symbol for m in l2_set}
        subs: list[Subscription] = []

        def wanted(switch: str | None, m: Market) -> bool:
            return switch == "all" or (switch == "l2" and m.symbol in l2_symbols)

        for m in ranked if ch.get("l2") == "all" else l2_set:
            if wanted(ch.get("l2"), m):
                subs.append(self._sub("l2Book", "l2", m.venue_id))
        if ch.get("l2") in ("all", "l2"):
            extra = set(self.cfg.l2.extra_sig_figs_for)
            for m in l2_set:
                if m.symbol not in extra:
                    continue
                for sf in self.cfg.l2.sig_figs:
                    subs.append(
                        Subscription(
                            key=f"l2Book:{m.venue_id}:sf{sf}",
                            stream=f"l2_sf{sf}",
                            message=_dumps(
                                {
                                    "method": "subscribe",
                                    "subscription": {"type": "l2Book", "coin": m.venue_id, "nSigFigs": sf},
                                }
                            ),
                            group=f"l2_sf{sf}",
                            classify_as="l2",
                        )
                    )
        for m in ranked:
            if wanted(ch.get("asset_ctx"), m):
                subs.append(self._sub("activeAssetCtx", "asset_ctx", m.venue_id))
            if wanted(ch.get("trades"), m):
                subs.append(self._sub("trades", "trades", m.venue_id))
            if wanted(ch.get("bbo"), m):
                subs.append(self._sub("bbo", "bbo", m.venue_id))
        return subs

    def _sub(self, typ: str, stream: str, coin: str) -> Subscription:
        return Subscription(
            key=f"{typ}:{coin}",
            stream=stream,
            message=_dumps({"method": "subscribe", "subscription": {"type": typ, "coin": coin}}),
        )

    def keepalive_message(self) -> str | None:
        return _dumps({"method": "ping"})

    def classify(self, text: str) -> Classified:
        try:
            msg = orjson.loads(text)
        except orjson.JSONDecodeError:
            # e.g. the plain-text greeting "Websocket connection established."
            return Classified(stream="control", channel="text", is_data=False)
        if not isinstance(msg, dict):
            return Classified(stream="unknown")
        channel = msg.get("channel")
        data = msg.get("data")
        if channel == "pong":
            return Classified(stream="control", channel="pong", is_data=False)
        if channel == "subscriptionResponse":
            return Classified(stream="control", channel=channel, is_data=False)
        if channel == "error":
            return Classified(stream="control", channel=channel, is_data=False, error=str(data))
        stream = _DATA_STREAMS.get(channel or "")
        if stream is None:
            return Classified(stream="unknown", channel=channel)
        market = None
        if isinstance(data, list):
            if data and isinstance(data[0], dict):
                market = data[0].get("coin")
        elif isinstance(data, dict):
            market = data.get("coin")
        return Classified(stream=stream, channel=channel, market=market)
