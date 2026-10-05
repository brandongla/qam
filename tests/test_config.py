from __future__ import annotations

from pathlib import Path

import pytest

from qam.recorders.config import load_config


def test_defaults_include_both_venues() -> None:
    cfg = load_config()
    assert set(cfg.venues) == {"hyperliquid", "lighter"}
    hl = cfg.venues["hyperliquid"]
    assert hl.ws_url.startswith("wss://") and hl.connection.max_subscriptions == 100
    assert cfg.venues["lighter"].channels["order_book"] == "l2"


def test_yaml_overrides_merge_with_venue_defaults(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(
        "data_root: /x\n"
        "archive: {flush_interval_s: 2}\n"
        "venues:\n"
        "  hyperliquid:\n"
        "    l2: {top_n_by_volume: 5}\n"
        "    connection: {stall_timeout_s: 30}\n"
    )
    cfg = load_config(p)
    assert cfg.data_root == "/x" and cfg.archive.flush_interval_s == 2
    assert list(cfg.venues) == ["hyperliquid"]  # venues listed in the file are the ones configured
    hl = cfg.venues["hyperliquid"]
    assert hl.l2.top_n_by_volume == 5 and hl.l2.markets == ["BTC", "ETH", "SOL"]
    assert hl.connection.stall_timeout_s == 30 and hl.connection.max_subscriptions == 100
    assert hl.ws_url == "wss://api.hyperliquid.xyz/ws"


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("venues:\n  lighter:\n    conection: {}\n")
    with pytest.raises(ValueError, match="conection"):
        load_config(p)


def test_example_deploy_config_loads() -> None:
    cfg = load_config(Path(__file__).parents[1] / "deploy" / "config" / "recorder.yaml")
    assert cfg.venues["hyperliquid"].enabled and cfg.venues["lighter"].enabled
