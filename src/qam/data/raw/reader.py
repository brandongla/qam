"""Readers and integrity verification for the raw archive."""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import orjson
import zstandard as zstd

from qam.data.raw.writer import DATA_SUFFIX, PART_SUFFIX, Manifest, sha256_file


def iter_file(path: Path | str, *, tolerant: bool = False) -> Iterator[dict[str, Any]]:
    """Yield envelopes from one raw file.

    With ``tolerant=True`` (used for crash recovery and for reading in-progress ``.part``
    files), a truncated trailing frame or partial last line ends iteration instead of raising.
    """
    path = Path(path)
    dctx = zstd.ZstdDecompressor()
    with path.open("rb") as fh:
        reader = dctx.stream_reader(fh, read_across_frames=True)
        text = io.BufferedReader(reader, buffer_size=1 << 20)  # type: ignore[arg-type]
        while True:
            try:
                line = text.readline()
            except zstd.ZstdError:
                if tolerant:
                    return
                raise
            if not line:
                return
            if not line.endswith(b"\n"):
                if tolerant:
                    return
                raise ValueError(f"{path}: truncated final line")
            try:
                yield orjson.loads(line)
            except orjson.JSONDecodeError:
                if tolerant:
                    return
                raise


def iter_records(
    root: Path | str,
    venue: str,
    stream: str,
    *,
    include_open: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield envelopes for a venue/stream in file-name (time) order."""
    base = Path(root) / "raw" / venue / stream
    if not base.exists():
        return
    pattern = f"*{DATA_SUFFIX}" + ("*" if include_open else "")
    for p in sorted(base.rglob(pattern)):
        yield from iter_file(p, tolerant=p.name.endswith(PART_SUFFIX))


@dataclass
class VerifyReport:
    files_checked: int = 0
    records_checked: int = 0
    ok: int = 0
    missing: list[str] = field(default_factory=list)
    checksum_mismatch: list[str] = field(default_factory=list)
    record_count_mismatch: list[str] = field(default_factory=list)
    unmanifested: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not (self.missing or self.checksum_mismatch or self.record_count_mismatch or self.unmanifested)


def verify_archive(root: Path | str, *, deep: bool = False, venue: str | None = None) -> VerifyReport:
    """Check every manifest entry against the file on disk.

    Always checks existence and SHA-256. With ``deep=True`` it also decodes every record and
    compares the record count. Also reports closed data files that have no manifest entry.
    """
    root = Path(root)
    rep = VerifyReport()
    entries = Manifest(root).entries(venue)
    seen: set[str] = set()
    for e in entries:
        rel = e["path"]
        seen.add(rel)
        p = root / rel
        rep.files_checked += 1
        if not p.exists():
            rep.missing.append(rel)
            continue
        if sha256_file(p) != e["sha256"]:
            rep.checksum_mismatch.append(rel)
            continue
        if deep:
            n = sum(1 for _ in iter_file(p))
            rep.records_checked += n
            if n != e["records"]:
                rep.record_count_mismatch.append(rel)
                continue
        rep.ok += 1
    raw = root / "raw"
    if raw.exists():
        globbed = raw / venue if venue else raw
        for p in globbed.rglob(f"*{DATA_SUFFIX}"):
            rel = p.relative_to(root).as_posix()
            if rel not in seen:
                rep.unmanifested.append(rel)
    return rep
