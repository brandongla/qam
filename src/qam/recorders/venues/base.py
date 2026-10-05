"""Venue adapter interface.

An adapter knows a venue's protocol details: market discovery, subscription messages,
keepalive, and how to cheaply classify a received message (channel, market, which archive
stream it goes to, whether it needs a reply). Adapters never transform the payload.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from qam.recorders.config import VenueConfig


@dataclass(frozen=True)
class Market:
    symbol: str  # venue symbol, e.g. "BTC"
    venue_id: str  # venue-native identifier (coin name, market index, ...)
    market_type: str  # "perp" | "spot"
    active: bool = True
    volume_24h_usd: float | None = None


@dataclass(frozen=True)
class Subscription:
    key: str  # unique identity, e.g. "trades:BTC"
    stream: str  # archive stream the resulting data lands in
    message: str  # exact text sent to subscribe
    # Connection group: subscriptions in different groups never share a connection.
    group: str = "main"
    # When set, messages the adapter classifies as stream ``classify_as`` on this subscription's
    # connection are written to ``stream`` instead. Used when a venue's messages can't
    # distinguish two variants of the same channel (e.g. Hyperliquid l2Book nSigFigs).
    classify_as: str | None = None


@dataclass
class Classified:
    stream: str  # archive stream: data stream name, "control", or "unknown"
    channel: str | None = None
    market: str | None = None
    is_data: bool = True  # counts toward connection liveness
    reply: str | None = None  # text to send back (e.g. application-level pong)
    ready: bool = False  # server signals it's ready for subscriptions
    seq: tuple[str, int] | None = None  # (sequence key, value) for monotonicity checks
    error: str | None = None  # venue-reported error


@dataclass
class DiscoveryResult:
    markets: list[Market]
    # Raw REST responses (endpoint, request body, response text), archived as reference data.
    raw_responses: list[tuple[str, str | None, str]] = field(default_factory=list)


class VenueAdapter(ABC):
    name: str = ""
    subscribe_on_open: bool = True  # False: wait for a Classified(ready=True) message
    keepalive_interval_s: float | None = None  # application-level keepalive interval

    def __init__(self, cfg: VenueConfig) -> None:
        self.cfg = cfg

    @property
    def ws_url(self) -> str:
        return self.cfg.ws_url

    @abstractmethod
    async def discover(self, client: httpx.AsyncClient) -> DiscoveryResult: ...

    @abstractmethod
    def plan(self, markets: list[Market]) -> list[Subscription]: ...

    @abstractmethod
    def classify(self, text: str) -> Classified: ...

    def keepalive_message(self) -> str | None:
        return None

    # -- shared helpers ---------------------------------------------------------------------

    def select_l2_markets(self, markets: list[Market], explicit: list[str], top_n: int) -> list[Market]:
        """Explicit symbols first, then the top-N remaining active markets by 24h volume."""
        active = [m for m in markets if m.active]
        by_symbol = {m.symbol: m for m in active}
        chosen: dict[str, Market] = {s: by_symbol[s] for s in explicit if s in by_symbol}
        ranked = sorted(
            (m for m in active if m.symbol not in chosen and m.volume_24h_usd is not None),
            key=lambda m: m.volume_24h_usd or 0.0,
            reverse=True,
        )
        for m in ranked[: max(0, top_n)]:
            chosen[m.symbol] = m
        return list(chosen.values())


def first_key(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d:
            return d[k]
    return None
