from __future__ import annotations

import os
from pathlib import Path

import zstandard as zstd

from qam.data.raw import RawArchive, iter_file, iter_records, make_message, make_meta, verify_archive
from qam.data.raw.writer import PART_SUFFIX

HOUR_NS = 3600 * 10**9
T0 = 1_760_000_000 * 10**9 - (1_760_000_000 * 10**9) % HOUR_NS  # an exact UTC hour boundary


def _msg(i: int, t: int) -> dict:
    return make_message(
        venue="testv", conn="c#1", seq=i, raw=f'{{"i":{i}}}', channel="trades", market="BTC", recv_ns=t
    )


def test_write_close_manifest_roundtrip(tmp_path: Path) -> None:
    ar = RawArchive(tmp_path)
    for i in range(100):
        ar.write("trades", _msg(i, T0 + i))
    closed = ar.close_all()
    assert len(closed) == 1
    cf = closed[0]
    assert cf.records == 100 and not cf.recovered
    assert cf.path.name.endswith(".jsonl.zst")
    recs = list(iter_records(tmp_path, "testv", "trades"))
    assert [r["seq"] for r in recs] == list(range(100))
    assert recs[0]["raw"] == '{"i":0}'  # raw text preserved exactly
    rep = verify_archive(tmp_path, deep=True)
    assert rep.passed and rep.ok == 1 and rep.records_checked == 100


def test_rotates_on_hour_boundary(tmp_path: Path) -> None:
    ar = RawArchive(tmp_path)
    ar.write("trades", _msg(0, T0 + HOUR_NS - 1))
    ar.write("trades", _msg(1, T0 + HOUR_NS))  # next hour -> new file
    ar.write("trades", _msg(2, T0 + HOUR_NS + 5))
    ar.close_all()
    entries = ar.manifest.entries("testv")
    assert sorted(e["records"] for e in entries) == [1, 2]
    assert len({e["hour"] for e in entries}) == 2
    assert verify_archive(tmp_path, deep=True).passed


def test_rotate_idle_closes_finished_hours(tmp_path: Path) -> None:
    ar = RawArchive(tmp_path)
    ar.write("trades", _msg(0, T0 + 10))
    assert ar.rotate_idle(now_ns=T0 + 20) == []  # same hour: stays open
    closed = ar.rotate_idle(now_ns=T0 + HOUR_NS + 1)
    assert len(closed) == 1


def test_rotates_on_size(tmp_path: Path) -> None:
    ar = RawArchive(tmp_path, max_bytes=2000)
    for i in range(200):
        ar.write("trades", _msg(i, T0 + i))
    ar.close_all()
    entries = ar.manifest.entries("testv")
    assert len(entries) > 1
    assert sum(e["records"] for e in entries) == 200
    assert [r["seq"] for r in iter_records(tmp_path, "testv", "trades")] == list(range(200))


def test_crash_recovery_keeps_flushed_frames_and_drops_torn_tail(tmp_path: Path) -> None:
    ar = RawArchive(tmp_path)
    w = ar.writer("testv", "trades")
    for i in range(50):
        w.write(_msg(i, T0 + i))
    w.flush()  # frame boundary: first 50 records are durable
    for i in range(50, 60):
        w.write(_msg(i, T0 + i))
    # Simulate a crash mid-frame: write some compressed bytes of an unfinished frame, then abandon.
    w._zw.flush(zstd.FLUSH_BLOCK)
    w._fh.flush()
    part = w._part
    w._fh.close()
    assert part is not None and part.name.endswith(PART_SUFFIX)
    # Tear the file: cut off the last few bytes of the unfinished frame.
    size = part.stat().st_size
    with part.open("r+b") as f:
        f.truncate(size - 3)

    ar2 = RawArchive(tmp_path)
    rec = ar2.recover()
    assert len(rec) == 1 and rec[0].recovered
    seqs = [r["seq"] for r in iter_file(rec[0].path)]
    assert seqs[:50] == list(range(50))  # everything before the last flush survives
    assert seqs == sorted(seqs)
    assert not list(tmp_path.rglob(f"*{PART_SUFFIX}"))
    assert verify_archive(tmp_path, deep=True).passed


def test_verify_detects_tampering_and_unmanifested(tmp_path: Path) -> None:
    ar = RawArchive(tmp_path)
    ar.write("trades", _msg(0, T0))
    cf = ar.close_all()[0]
    with cf.path.open("ab") as f:
        f.write(b"x")
    stray = cf.path.with_name("stray.jsonl.zst")
    stray.write_bytes(b"")
    rep = verify_archive(tmp_path)
    assert not rep.passed
    assert rep.checksum_mismatch and rep.unmanifested


def test_meta_records_roundtrip(tmp_path: Path) -> None:
    ar = RawArchive(tmp_path)
    ar.write("meta", make_meta(venue="testv", conn="c#1", event="disconnect", info={"reason": "x"}))
    ar.close_all()
    recs = list(iter_records(tmp_path, "testv", "meta"))
    assert recs[0]["k"] == "meta" and recs[0]["info"] == {"reason": "x"}


def test_empty_part_is_removed_on_recovery(tmp_path: Path) -> None:
    d = tmp_path / "raw" / "testv" / "trades" / "2026-01-01"
    d.mkdir(parents=True)
    p = d / f"testv.trades.20260101T00.1.jsonl.zst{PART_SUFFIX}"
    p.write_bytes(b"")
    assert RawArchive(tmp_path).recover() == []
    assert not os.path.exists(p)


def test_recover_only_touches_owned_venues(tmp_path: Path) -> None:
    other = RawArchive(tmp_path)
    w = other.writer("othervenue", "trades")
    w.write(_msg(0, T0))
    w.flush()  # live .part file belonging to another process
    assert RawArchive(tmp_path).recover(["testv"]) == []
    assert list(tmp_path.rglob(f"*{PART_SUFFIX}"))  # untouched
    w.close()
