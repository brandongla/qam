"""Crash-safe rotating writer for the raw archive.

Durability model:
- Records go into a ``.part`` file through a zstd stream. Every ``flush_interval_s`` the current
  zstd frame is closed and the file is flushed and fsynced. A crash therefore loses at most the
  last ``flush_interval_s`` of data, and everything before it stays decodable.
- Files rotate at UTC hour boundaries (by the record's ``recv_ns``) or when ``max_bytes`` is
  exceeded. On rotation the file is finalized: frame closed, fsynced, renamed to its final name,
  SHA-256 computed, and a manifest entry appended (and fsynced).
- On startup, ``RawArchive.recover()`` finalizes ``.part`` files left by a crash. Their decodable
  records are re-compressed into a clean file, and the manifest entry carries ``recovered: true``.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import orjson
import zstandard as zstd

WRITER_VERSION = 1
PART_SUFFIX = ".part"
DATA_SUFFIX = ".jsonl.zst"


def _hour_key(recv_ns: int) -> str:
    # Integer division: float(recv_ns) / 1e9 loses ns precision and can misplace records at
    # hour boundaries.
    return datetime.fromtimestamp(recv_ns // 1_000_000_000, tz=UTC).strftime("%Y%m%dT%H")


def _day_of_hour_key(hour_key: str) -> str:
    return f"{hour_key[0:4]}-{hour_key[4:6]}-{hour_key[6:8]}"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@dataclass
class ClosedFile:
    path: Path
    venue: str
    stream: str
    hour: str
    records: int
    bytes: int
    sha256: str
    first_recv_ns: int | None
    last_recv_ns: int | None
    recovered: bool = False


class Manifest:
    """Append-only manifest of closed files, one JSONL file per venue per UTC day."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, venue: str, day: str) -> Path:
        return self.root / "manifest" / venue / f"{day}.jsonl"

    def append(self, cf: ClosedFile) -> None:
        day = _day_of_hour_key(cf.hour)
        mpath = self.path_for(cf.venue, day)
        mpath.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "path": cf.path.relative_to(self.root).as_posix(),
            "venue": cf.venue,
            "stream": cf.stream,
            "hour": cf.hour,
            "records": cf.records,
            "bytes": cf.bytes,
            "sha256": cf.sha256,
            "first_recv_ns": cf.first_recv_ns,
            "last_recv_ns": cf.last_recv_ns,
            "recovered": cf.recovered,
            "writer_version": WRITER_VERSION,
            "closed_ns": time.time_ns(),
        }
        with mpath.open("ab") as f:
            f.write(orjson.dumps(entry) + b"\n")
            f.flush()
            os.fsync(f.fileno())

    def entries(self, venue: str | None = None) -> list[dict[str, Any]]:
        base = self.root / "manifest"
        if not base.exists():
            return []
        out: list[dict[str, Any]] = []
        venues = [base / venue] if venue else sorted(p for p in base.iterdir() if p.is_dir())
        for vdir in venues:
            if not vdir.exists():
                continue
            for mfile in sorted(vdir.glob("*.jsonl")):
                with mfile.open("rb") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            out.append(orjson.loads(line))
        return out


class RawStreamWriter:
    """Writes envelopes for one (venue, stream) into hourly rotating zstd JSONL files."""

    def __init__(
        self,
        root: Path,
        venue: str,
        stream: str,
        manifest: Manifest,
        *,
        max_bytes: int = 512 * 1024 * 1024,
        flush_interval_s: float = 5.0,
        level: int = 3,
        on_close: Callable[[ClosedFile], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = root
        self.venue = venue
        self.stream = stream
        self.manifest = manifest
        self.max_bytes = max_bytes
        self.flush_interval_s = flush_interval_s
        self.level = level
        self.on_close = on_close
        self._clock = clock

        self._fh: IO[bytes] | None = None
        self._zw: Any = None
        self._part: Path | None = None
        self._hour: str | None = None
        self._records = 0
        self._uncompressed = 0
        self._first_ns: int | None = None
        self._last_ns: int | None = None
        self._last_flush = self._clock()
        self._dirty = False

    # -- public API -------------------------------------------------------------------------

    def write(self, record: dict[str, Any]) -> None:
        recv_ns = int(record["recv_ns"])
        hour = _hour_key(recv_ns)
        if self._fh is None:
            self._open(hour, recv_ns)
        elif hour != self._hour or self._uncompressed >= self.max_bytes:
            self.close()
            self._open(hour, recv_ns)
        line = orjson.dumps(record) + b"\n"
        self._zw.write(line)
        self._records += 1
        self._uncompressed += len(line)
        if self._first_ns is None:
            self._first_ns = recv_ns
        self._last_ns = recv_ns
        self._dirty = True
        if self._clock() - self._last_flush >= self.flush_interval_s:
            self.flush()

    def flush(self) -> None:
        """End the current zstd frame and fsync, making all records so far crash-durable."""
        if self._fh is None or not self._dirty:
            self._last_flush = self._clock()
            return
        self._zw.flush(zstd.FLUSH_FRAME)
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._dirty = False
        self._last_flush = self._clock()

    def close(self) -> ClosedFile | None:
        if self._fh is None:
            return None
        assert self._part is not None and self._hour is not None
        self._zw.flush(zstd.FLUSH_FRAME)
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._zw.close()  # closefd=False: leaves self._fh open
        self._fh.close()
        final = self._part.with_name(self._part.name[: -len(PART_SUFFIX)])
        os.replace(self._part, final)
        _fsync_dir(final.parent)
        cf = ClosedFile(
            path=final,
            venue=self.venue,
            stream=self.stream,
            hour=self._hour,
            records=self._records,
            bytes=final.stat().st_size,
            sha256=sha256_file(final),
            first_recv_ns=self._first_ns,
            last_recv_ns=self._last_ns,
        )
        self.manifest.append(cf)
        self._reset()
        if self.on_close:
            self.on_close(cf)
        return cf

    @property
    def is_open(self) -> bool:
        return self._fh is not None

    # -- internals --------------------------------------------------------------------------

    def _open(self, hour: str, recv_ns: int) -> None:
        day = _day_of_hour_key(hour)
        d = self.root / "raw" / self.venue / self.stream / day
        d.mkdir(parents=True, exist_ok=True)
        name = f"{self.venue}.{self.stream}.{hour}.{recv_ns}{DATA_SUFFIX}{PART_SUFFIX}"
        self._part = d / name
        self._fh = self._part.open("xb")
        self._zw = zstd.ZstdCompressor(level=self.level).stream_writer(self._fh, closefd=False)
        self._hour = hour
        self._last_flush = self._clock()

    def _reset(self) -> None:
        self._fh = None
        self._zw = None
        self._part = None
        self._hour = None
        self._records = 0
        self._uncompressed = 0
        self._first_ns = None
        self._last_ns = None
        self._dirty = False


class RawArchive:
    """Owns one writer per (venue, stream) under a common root, plus the manifest."""

    def __init__(
        self,
        root: Path | str,
        *,
        max_bytes: int = 512 * 1024 * 1024,
        flush_interval_s: float = 5.0,
        level: int = 3,
        on_close: Callable[[ClosedFile], None] | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = Manifest(self.root)
        self._kw = dict(max_bytes=max_bytes, flush_interval_s=flush_interval_s, level=level)
        self._on_close = on_close
        self._writers: dict[tuple[str, str], RawStreamWriter] = {}

    def writer(self, venue: str, stream: str) -> RawStreamWriter:
        key = (venue, stream)
        w = self._writers.get(key)
        if w is None:
            w = RawStreamWriter(self.root, venue, stream, self.manifest, on_close=self._on_close, **self._kw)
            self._writers[key] = w
        return w

    def write(self, stream: str, record: dict[str, Any]) -> None:
        self.writer(record["venue"], stream).write(record)

    def flush_all(self) -> None:
        for w in self._writers.values():
            w.flush()

    def close_all(self) -> list[ClosedFile]:
        closed = [cf for w in self._writers.values() if (cf := w.close()) is not None]
        return closed

    def rotate_idle(self, now_ns: int | None = None) -> list[ClosedFile]:
        """Close files whose hour has ended even if no new record arrived (quiet streams)."""
        current = _hour_key(now_ns if now_ns is not None else time.time_ns())
        out = []
        for w in self._writers.values():
            if w.is_open and w._hour != current and (cf := w.close()) is not None:
                out.append(cf)
        return out

    def recover(self, venues: list[str] | None = None) -> list[ClosedFile]:
        """Finalize ``.part`` files left behind by a crash (call before writing).

        Pass ``venues`` to restrict recovery to venues this process owns. Another process may be
        actively writing ``.part`` files for other venues under the same root.
        """
        from qam.data.raw.reader import iter_file

        recovered: list[ClosedFile] = []
        raw_dir = self.root / "raw"
        if not raw_dir.exists():
            return recovered
        dirs = [raw_dir / v for v in venues] if venues is not None else [raw_dir]
        parts = sorted(p for d in dirs if d.exists() for p in d.rglob(f"*{DATA_SUFFIX}{PART_SUFFIX}"))
        for part in parts:
            venue, stream, hour = part.name.split(".")[0:3]
            records = list(iter_file(part, tolerant=True))
            final = part.with_name(part.name[: -len(PART_SUFFIX)])
            if not records:
                part.unlink()
                continue
            tmp = final.with_name(final.name + ".recover")
            with tmp.open("wb") as fh:
                zw = zstd.ZstdCompressor(level=self._kw["level"]).stream_writer(fh, closefd=False)
                for r in records:
                    zw.write(orjson.dumps(r) + b"\n")
                zw.flush(zstd.FLUSH_FRAME)
                zw.close()
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, final)
            part.unlink()
            _fsync_dir(final.parent)
            cf = ClosedFile(
                path=final,
                venue=venue,
                stream=stream,
                hour=hour,
                records=len(records),
                bytes=final.stat().st_size,
                sha256=sha256_file(final),
                first_recv_ns=records[0].get("recv_ns"),
                last_recv_ns=records[-1].get("recv_ns"),
                recovered=True,
            )
            self.manifest.append(cf)
            recovered.append(cf)
        return recovered
