"""Raw record envelope.

Every line in a raw file is one JSON object:

``msg`` records (a message received from a venue)::

    {"v": 1, "k": "msg", "recv_ns": ..., "mono_ns": ..., "venue": "hyperliquid",
     "conn": "hyperliquid-0#3", "seq": 17, "ch": "trades", "mkt": "BTC", "raw": "<exact text>"}

``meta`` records (events from the recorder itself: connects, disconnects, subscriptions,
anomalies). Gaps are data: downstream processing derives coverage windows from these::

    {"v": 1, "k": "meta", "recv_ns": ..., "mono_ns": ..., "venue": "...", "conn": "...",
     "event": "disconnect", "info": {...}}

``raw`` is the exact text received, never re-serialized, so the archive is byte-faithful to
the source. ``ch``/``mkt`` are best-effort classification for partitioning and indexing only;
parsing into canonical schemas happens offline, in versioned normalizers.
"""

from __future__ import annotations

import time
from typing import Any

ENVELOPE_VERSION = 1


def make_message(
    *,
    venue: str,
    conn: str,
    seq: int,
    raw: str,
    channel: str | None,
    market: str | None,
    recv_ns: int | None = None,
    mono_ns: int | None = None,
) -> dict[str, Any]:
    return {
        "v": ENVELOPE_VERSION,
        "k": "msg",
        "recv_ns": recv_ns if recv_ns is not None else time.time_ns(),
        "mono_ns": mono_ns if mono_ns is not None else time.monotonic_ns(),
        "venue": venue,
        "conn": conn,
        "seq": seq,
        "ch": channel,
        "mkt": market,
        "raw": raw,
    }


def make_meta(
    *,
    venue: str,
    conn: str | None,
    event: str,
    info: dict[str, Any] | None = None,
    recv_ns: int | None = None,
    mono_ns: int | None = None,
) -> dict[str, Any]:
    return {
        "v": ENVELOPE_VERSION,
        "k": "meta",
        "recv_ns": recv_ns if recv_ns is not None else time.time_ns(),
        "mono_ns": mono_ns if mono_ns is not None else time.monotonic_ns(),
        "venue": venue,
        "conn": conn,
        "event": event,
        "info": info or {},
    }
