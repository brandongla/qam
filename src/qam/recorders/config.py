"""Recorder configuration (YAML). See deploy/config/recorder.yaml for a documented example."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, TypeVar

import yaml

T = TypeVar("T")


@dataclass
class ConnectionConfig:
    max_subscriptions: int = 100  # per websocket connection (shard size)
    max_total_subscriptions: int = 950  # venue-wide safety cap
    subscribe_rate_per_s: float = 20.0  # venue-wide pacing of outgoing subscribe messages
    stall_timeout_s: float = 60.0  # reconnect if no data message for this long
    open_timeout_s: float = 15.0
    ping_interval_s: float = 20.0  # websocket protocol-level ping
    ping_timeout_s: float = 20.0
    backoff_initial_s: float = 1.0
    backoff_max_s: float = 60.0
    max_message_bytes: int = 32 * 1024 * 1024


@dataclass
class L2Config:
    markets: list[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    top_n_by_volume: int = 20
    # Hyperliquid only: additional coarser aggregations (nSigFigs) for a deeper view of the book.
    extra_sig_figs_for: list[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    sig_figs: list[int] = field(default_factory=lambda: [4, 3])


@dataclass
class VenueConfig:
    name: str = ""
    enabled: bool = True
    ws_url: str = ""
    rest_url: str = ""
    market_types: list[str] = field(default_factory=lambda: ["perp"])
    # Channel switches: "all" (every active market), "none", or "l2" (only the L2 market set).
    channels: dict[str, str] = field(default_factory=dict)
    l2: L2Config = field(default_factory=L2Config)
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    refresh_markets_s: float = 6 * 3600


@dataclass
class ArchiveConfig:
    flush_interval_s: float = 5.0
    max_file_mb: int = 512
    zstd_level: int = 3


@dataclass
class RecorderConfig:
    data_root: str = "./data"
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    health_interval_s: float = 10.0
    venues: dict[str, VenueConfig] = field(default_factory=dict)


DEFAULT_VENUES: dict[str, dict[str, Any]] = {
    "hyperliquid": {
        "ws_url": "wss://api.hyperliquid.xyz/ws",
        "rest_url": "https://api.hyperliquid.xyz",
        "channels": {"trades": "all", "bbo": "all", "asset_ctx": "all", "l2": "l2"},
        "l2": {"markets": ["BTC", "ETH", "SOL"], "top_n_by_volume": 20},
        "connection": {"max_subscriptions": 100, "max_total_subscriptions": 950, "subscribe_rate_per_s": 20},
    },
    "lighter": {
        "ws_url": "wss://mainnet.zklighter.elliot.ai/stream",
        "rest_url": "https://mainnet.zklighter.elliot.ai",
        "channels": {"trades": "all", "market_stats": "all", "order_book": "l2"},
        "l2": {"markets": ["BTC", "ETH", "SOL"], "top_n_by_volume": 30},
        "connection": {"max_subscriptions": 50, "max_total_subscriptions": 600, "subscribe_rate_per_s": 10},
    },
}


def _build(cls: type[T], data: dict[str, Any] | None) -> T:
    data = dict(data or {})
    kwargs: dict[str, Any] = {}
    known = {f.name: f for f in fields(cls)}  # type: ignore[arg-type]
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"unknown config keys for {cls.__name__}: {sorted(unknown)}")
    for name, value in data.items():
        f = known[name]
        default_factory = f.default_factory  # type: ignore[misc]
        sample = default_factory() if callable(default_factory) else f.default
        if is_dataclass(sample) and isinstance(value, dict):
            kwargs[name] = _build(type(sample), value)
        else:
            kwargs[name] = value
    return cls(**kwargs)


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: Path | str | None = None, overrides: dict[str, Any] | None = None) -> RecorderConfig:
    raw: dict[str, Any] = {}
    if path is not None:
        with Path(path).open() as f:
            raw = yaml.safe_load(f) or {}
    if overrides:
        raw = _deep_merge(raw, overrides)
    venues_raw = raw.pop("venues", None)
    if venues_raw is None:
        venues_raw = {name: {} for name in DEFAULT_VENUES}
    cfg = _build(RecorderConfig, raw)
    for name, vraw in venues_raw.items():
        merged = _deep_merge(DEFAULT_VENUES.get(name, {}), vraw or {})
        merged["name"] = name
        cfg.venues[name] = _build(VenueConfig, merged)
    return cfg
