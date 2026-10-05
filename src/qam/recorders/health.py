"""Recorder health state, periodically written to ``<data_root>/health/<venue>.json``."""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import orjson


@dataclass
class ConnStats:
    shard: str
    state: str = "init"  # init | connecting | open | subscribed | closed
    subs: int = 0
    connected_since_ns: int | None = None
    last_msg_ns: int | None = None
    last_data_ns: int | None = None
    msgs: int = 0
    data_msgs: int = 0
    last_error: str | None = None


@dataclass
class StreamStats:
    records: int = 0
    last_recv_ns: int | None = None


@dataclass
class VenueHealth:
    venue: str
    run_id: str
    pid: int = field(default_factory=os.getpid)
    started_ns: int = field(default_factory=time.time_ns)
    connections: dict[str, ConnStats] = field(default_factory=dict)  # by shard id (latest epoch)
    streams: dict[str, StreamStats] = field(default_factory=dict)
    reconnects: int = 0
    reconnect_reasons: dict[str, int] = field(default_factory=dict)
    unknown_msgs: int = 0
    venue_errors: int = 0
    seq_regressions: int = 0
    files_closed: int = 0
    bytes_closed: int = 0
    markets: int = 0
    subscriptions_planned: int = 0
    subscriptions_dropped: int = 0

    def conn(self, shard: str) -> ConnStats:
        cs = self.connections.get(shard)
        if cs is None:
            cs = self.connections[shard] = ConnStats(shard=shard)
        return cs

    def stream(self, name: str) -> StreamStats:
        st = self.streams.get(name)
        if st is None:
            st = self.streams[name] = StreamStats()
        return st

    def note_reconnect(self, reason: str) -> None:
        self.reconnects += 1
        key = reason.split(":")[0][:60]
        self.reconnect_reasons[key] = self.reconnect_reasons.get(key, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        d = asdict(self)
        d["updated_ns"] = time.time_ns()
        return d

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(orjson.dumps(self.snapshot(), option=orjson.OPT_INDENT_2))
        os.replace(tmp, path)


def read_health(path: Path) -> dict[str, Any]:
    return orjson.loads(path.read_bytes())
