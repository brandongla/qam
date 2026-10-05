from __future__ import annotations

from pathlib import Path

import pytest

from qam.cli import main
from qam.data.raw import RawArchive, make_message
from qam.recorders.health import VenueHealth


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    ar = RawArchive(tmp_path)
    for i in range(3):
        ar.write(
            "trades",
            make_message(venue="hyperliquid", conn="c", seq=i, raw="{}", channel="trades", market="BTC"),
        )
    ar.close_all()
    return tmp_path


def test_raw_verify_summary_cat(archive_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["raw", "verify", "--data-root", str(archive_root), "--deep"]) == 0
    assert "PASS" in capsys.readouterr().out
    assert main(["raw", "summary", "--data-root", str(archive_root)]) == 0
    out = capsys.readouterr().out
    assert "hyperliquid" in out and "trades" in out
    f = next(archive_root.rglob("*.jsonl.zst"))
    assert main(["raw", "cat", str(f), "--limit", "2"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 2


def test_raw_verify_fails_on_corruption(archive_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = next(archive_root.rglob("*.jsonl.zst"))
    f.write_bytes(f.read_bytes()[:-1])
    assert main(["raw", "verify", "--data-root", str(archive_root)]) == 1
    assert "CHECKSUM MISMATCH" in capsys.readouterr().out


def test_record_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    h = VenueHealth(venue="lighter", run_id="abc")
    c = h.conn("main-0")
    c.state, c.subs = "subscribed", 3
    h.write(tmp_path / "health" / "lighter.json")
    rc = main(["record", "status", "--data-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "lighter" in out and "main-0" in out
    assert rc == 1  # no data received yet -> unhealthy
    assert main(["record", "status", "--data-root", str(tmp_path / "nope")]) == 1
