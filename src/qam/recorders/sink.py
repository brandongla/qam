"""Bridges connections to the raw archive and keeps health counters in step."""

from __future__ import annotations

import time
from typing import Any

from qam.data.raw import RawArchive, make_message, make_meta
from qam.recorders.health import VenueHealth


class RecorderSink:
    def __init__(self, archive: RawArchive, venue: str, health: VenueHealth) -> None:
        self.archive = archive
        self.venue = venue
        self.health = health

    def message(
        self,
        *,
        conn: str,
        seq: int,
        raw: str,
        stream: str,
        channel: str | None,
        market: str | None,
        recv_ns: int | None = None,
        mono_ns: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        rec = make_message(
            venue=self.venue,
            conn=conn,
            seq=seq,
            raw=raw,
            channel=channel,
            market=market,
            recv_ns=recv_ns,
            mono_ns=mono_ns,
        )
        if extra:
            rec.update(extra)
        self.archive.write(stream, rec)
        st = self.health.stream(stream)
        st.records += 1
        st.last_recv_ns = rec["recv_ns"]

    def meta(self, conn: str | None, event: str, info: dict[str, Any] | None = None) -> None:
        self.archive.write("meta", make_meta(venue=self.venue, conn=conn, event=event, info=info))

    def reference(self, conn: str, url: str, body: str | None, text: str) -> None:
        """Archive a raw REST response (market metadata) as reference data."""
        self.message(
            conn=conn,
            seq=0,
            raw=text,
            stream="reference",
            channel=url,
            market=None,
            recv_ns=time.time_ns(),
            extra={"req": body},
        )
