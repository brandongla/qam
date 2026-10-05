"""Command-line interface.

qam record run    --config CFG [--venue V ...]      run recorders until stopped
qam record smoke  --venue V [--seconds N]          short live run + pass/fail summary
qam record plan   --config CFG --venue V           discover markets and print the plan
qam record status --data-root DIR                  print recorder health
qam raw verify    --data-root DIR [--deep]         check files against the manifest
qam raw summary   --data-root DIR                  files/records/bytes per venue/stream
qam raw cat       FILE [--limit N]                 print records from one raw file
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
import orjson

from qam.data.raw import iter_file, verify_archive
from qam.data.raw.writer import Manifest
from qam.recorders.config import load_config
from qam.recorders.health import read_health


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):  # non-Unix
            loop.add_signal_handler(sig, stop.set)


# -- record -----------------------------------------------------------------------------------


def cmd_record_run(args: argparse.Namespace) -> int:
    from qam.recorders.runner import run_recorders

    overrides = {"data_root": args.data_root} if args.data_root else None
    cfg = load_config(args.config, overrides)

    async def main() -> None:
        stop = asyncio.Event()
        _install_signal_handlers(stop)
        await run_recorders(cfg, args.venue or None, stop)

    asyncio.run(main())
    return 0


def cmd_record_smoke(args: argparse.Namespace) -> int:
    """Run one venue live for N seconds into a scratch directory and report what arrived."""
    from qam.recorders.runner import run_recorders

    root = Path(args.data_root) if args.data_root else Path(tempfile.mkdtemp(prefix="qam-smoke-"))
    cfg = load_config(args.config, {"data_root": str(root), "health_interval_s": 2})
    for name, v in cfg.venues.items():
        v.enabled = name == args.venue

    async def main() -> dict[str, Any]:
        stop = asyncio.Event()
        _install_signal_handlers(stop)
        healths = await run_recorders(cfg, [args.venue], stop, duration_s=args.seconds)
        return healths[args.venue].snapshot()

    snap = asyncio.run(main())
    streams = {k: v["records"] for k, v in snap["streams"].items()}
    data_streams = {k: n for k, n in streams.items() if k not in ("meta", "control", "unknown", "reference")}
    print(f"data root: {root}")
    print(f"markets discovered: {snap['markets']}, subscriptions planned: {snap['subscriptions_planned']}")
    print(f"records per stream: {orjson.dumps(streams, option=orjson.OPT_SORT_KEYS).decode()}")
    print(
        f"reconnects: {snap['reconnects']} {snap['reconnect_reasons']}  unknown: {snap['unknown_msgs']}  "
        f"venue errors: {snap['venue_errors']}  seq regressions: {snap['seq_regressions']}"
    )
    rep = verify_archive(root, deep=True)
    print(
        f"archive verify: {'PASS' if rep.passed else 'FAIL'} ({rep.ok} files, {rep.records_checked} records)"
    )
    ok = bool(data_streams) and sum(data_streams.values()) > 0 and rep.passed and snap["venue_errors"] == 0
    if snap["unknown_msgs"]:
        print("note: unknown messages were recorded. Inspect the 'unknown' stream with `qam raw cat`.")
    print("SMOKE TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def cmd_record_plan(args: argparse.Namespace) -> int:
    from qam.recorders.venues import ADAPTERS

    cfg = load_config(args.config)
    vcfg = cfg.venues[args.venue]
    adapter = ADAPTERS[args.venue](vcfg)

    async def main() -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await adapter.discover(client)
        plan = adapter.plan(res.markets)
        by_stream: dict[str, int] = defaultdict(int)
        for s in plan:
            by_stream[s.stream] += 1
        active = sum(1 for m in res.markets if m.active)
        print(f"{args.venue}: {len(res.markets)} markets ({active} active), {len(plan)} subscriptions")
        print("per stream:", dict(by_stream))
        cap = vcfg.connection.max_total_subscriptions
        if len(plan) > cap:
            print(f"WARNING: plan exceeds max_total_subscriptions={cap}; {len(plan) - cap} would be dropped")
        if args.verbose:
            for s in plan:
                print(f"  {s.group:10} {s.stream:12} {s.key}")

    asyncio.run(main())
    return 0


def cmd_record_status(args: argparse.Namespace) -> int:
    hdir = Path(args.data_root) / "health"
    files = sorted(hdir.glob("*.json")) if hdir.exists() else []
    if not files:
        print(f"no health files under {hdir}")
        return 1
    now = time.time_ns()
    worst = 0
    for f in files:
        h = read_health(f)
        age = (now - h["updated_ns"]) / 1e9
        print(f"== {h['venue']} (run {h['run_id']}, pid {h['pid']}), updated {age:.0f}s ago")
        if age > 120:
            print("   WARNING: health file is stale. Is the recorder running?")
            worst = 1
        print(
            f"   markets {h['markets']}  subs {h['subscriptions_planned']} "
            f"(dropped {h['subscriptions_dropped']})  "
            f"reconnects {h['reconnects']}  unknown {h['unknown_msgs']}  errors {h['venue_errors']}  "
            f"files closed {h['files_closed']} ({h['bytes_closed'] / 1e6:.1f} MB)"
        )
        for shard, c in sorted(h["connections"].items()):
            last = c.get("last_data_ns")
            lag = f"{(now - last) / 1e9:.0f}s" if last else "never"
            print(
                f"   {shard:14} {c['state']:10} subs {c['subs']:4}  "
                f"data msgs {c['data_msgs']:9}  last data {lag}"
            )
            if c["state"] != "subscribed" or not last or (now - last) / 1e9 > 300:
                worst = 1
        for name, st in sorted(h["streams"].items()):
            last = st.get("last_recv_ns")
            lag = f"{(now - last) / 1e9:.0f}s ago" if last else "never"
            print(f"   stream {name:12} records {st['records']:10}  last {lag}")
    return worst


# -- raw --------------------------------------------------------------------------------------


def cmd_raw_verify(args: argparse.Namespace) -> int:
    rep = verify_archive(args.data_root, deep=args.deep, venue=args.venue)
    print(f"checked {rep.files_checked} files ({rep.records_checked} records decoded): {rep.ok} ok")
    for label, items in (
        ("MISSING", rep.missing),
        ("CHECKSUM MISMATCH", rep.checksum_mismatch),
        ("RECORD COUNT MISMATCH", rep.record_count_mismatch),
        ("NOT IN MANIFEST", rep.unmanifested),
    ):
        for p in items:
            print(f"{label}: {p}")
    print("PASS" if rep.passed else "FAIL")
    return 0 if rep.passed else 1


def cmd_raw_summary(args: argparse.Namespace) -> int:
    entries = Manifest(Path(args.data_root)).entries(args.venue)
    agg: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    last_hour: dict[tuple[str, str], str] = {}
    for e in entries:
        k = (e["venue"], e["stream"])
        agg[k][0] += 1
        agg[k][1] += e["records"]
        agg[k][2] += e["bytes"]
        last_hour[k] = max(last_hour.get(k, ""), e["hour"])
    print(f"{'venue':12} {'stream':12} {'files':>6} {'records':>12} {'MB':>10}  last hour")
    for (venue, stream), (n, recs, b) in sorted(agg.items()):
        print(f"{venue:12} {stream:12} {n:6} {recs:12} {b / 1e6:10.1f}  {last_hour[(venue, stream)]}")
    return 0


def cmd_raw_cat(args: argparse.Namespace) -> int:
    for i, rec in enumerate(iter_file(args.file, tolerant=args.file.endswith(".part"))):
        if args.limit and i >= args.limit:
            break
        sys.stdout.write(orjson.dumps(rec).decode() + "\n")
    return 0


# -- entry point ------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qam", description="QAM command-line interface")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="group", required=True)

    rec = sub.add_parser("record", help="market data recorders").add_subparsers(dest="cmd", required=True)
    r = rec.add_parser("run", help="run recorders until stopped (SIGINT/SIGTERM)")
    r.add_argument("--config", help="recorder YAML config (defaults built in)")
    r.add_argument("--venue", action="append", help="venue to run (repeatable); default: all enabled")
    r.add_argument("--data-root", help="override data_root from config")
    r.set_defaults(func=cmd_record_run)

    r = rec.add_parser("smoke", help="short live test of one venue")
    r.add_argument("--venue", required=True)
    r.add_argument("--seconds", type=float, default=60.0)
    r.add_argument("--config")
    r.add_argument("--data-root", help="where to write (default: a new temp dir)")
    r.set_defaults(func=cmd_record_smoke)

    r = rec.add_parser("plan", help="discover markets and show the subscription plan")
    r.add_argument("--venue", required=True)
    r.add_argument("--config")
    r.set_defaults(func=cmd_record_plan)

    r = rec.add_parser("status", help="show recorder health")
    r.add_argument("--data-root", required=True)
    r.set_defaults(func=cmd_record_status)

    raw = sub.add_parser("raw", help="raw archive tools").add_subparsers(dest="cmd", required=True)
    r = raw.add_parser("verify", help="verify files against the manifest")
    r.add_argument("--data-root", required=True)
    r.add_argument("--venue")
    r.add_argument("--deep", action="store_true", help="also decode every record")
    r.set_defaults(func=cmd_raw_verify)

    r = raw.add_parser("summary", help="summarize the archive")
    r.add_argument("--data-root", required=True)
    r.add_argument("--venue")
    r.set_defaults(func=cmd_raw_summary)

    r = raw.add_parser("cat", help="print records from a raw file")
    r.add_argument("file")
    r.add_argument("--limit", type=int, default=0)
    r.set_defaults(func=cmd_raw_cat)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
