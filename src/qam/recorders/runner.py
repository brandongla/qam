"""Runs all shards for one venue: discovery, planning, sharding, refresh, housekeeping."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from qam.data.raw import RawArchive
from qam.data.raw.writer import ClosedFile
from qam.recorders.config import RecorderConfig, VenueConfig
from qam.recorders.health import VenueHealth
from qam.recorders.sink import RecorderSink
from qam.recorders.venues import ADAPTERS
from qam.recorders.venues.base import Market, Subscription, VenueAdapter
from qam.recorders.ws import Backoff, RateLimiter, WsShard

log = logging.getLogger("qam.recorder")


class VenueLock:
    """Exclusive per-venue lock under ``<data_root>/locks`` so two recorders can't own one venue."""

    def __init__(self, root: Path, venue: str) -> None:
        import fcntl

        d = root / "locks"
        d.mkdir(parents=True, exist_ok=True)
        self._fh = (d / f"{venue}.lock").open("a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._fh.close()
            raise RuntimeError(f"another recorder is already running for venue {venue!r} in {root}") from None
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"{os.getpid()}\n")
        self._fh.flush()

    def release(self) -> None:
        if not self._fh.closed:
            self._fh.close()


def make_archive(cfg: RecorderConfig, health_by_venue: dict[str, VenueHealth]) -> RawArchive:
    def on_close(cf: ClosedFile) -> None:
        h = health_by_venue.get(cf.venue)
        if h is not None:
            h.files_closed += 1
            h.bytes_closed += cf.bytes

    return RawArchive(
        cfg.data_root,
        max_bytes=cfg.archive.max_file_mb * 1024 * 1024,
        flush_interval_s=cfg.archive.flush_interval_s,
        level=cfg.archive.zstd_level,
        on_close=on_close,
    )


def shard_subscriptions(subs: list[Subscription], max_per_conn: int) -> dict[str, list[Subscription]]:
    """Group by connection group, then chunk. Shard ids are stable for a given plan order."""
    groups: dict[str, list[Subscription]] = {}
    for s in subs:
        groups.setdefault(s.group, []).append(s)
    shards: dict[str, list[Subscription]] = {}
    for group, items in groups.items():
        for i in range(0, len(items), max(1, max_per_conn)):
            shards[f"{group}-{i // max(1, max_per_conn)}"] = items[i : i + max_per_conn]
    return shards


class VenueRecorder:
    def __init__(
        self,
        cfg: RecorderConfig,
        vcfg: VenueConfig,
        archive: RawArchive,
        health: VenueHealth,
        *,
        adapter: VenueAdapter | None = None,
        http_client: Callable[[], httpx.AsyncClient] | None = None,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.vcfg = vcfg
        self.archive = archive
        self.health = health
        self.adapter = adapter or ADAPTERS[vcfg.name](vcfg)
        self.sink = RecorderSink(archive, self.adapter.name, health)
        self.limiter = RateLimiter(vcfg.connection.subscribe_rate_per_s)
        self._http = http_client or (lambda: httpx.AsyncClient(timeout=30.0))
        self._connect = connect
        self.shards: dict[str, WsShard] = {}
        self._shard_tasks: dict[str, asyncio.Task[None]] = {}
        self._planned_keys: set[str] = set()
        self._next_extra = 0

    @property
    def venue(self) -> str:
        return self.adapter.name

    async def discover(self, stop: asyncio.Event) -> list[Market] | None:
        backoff = Backoff(2.0, 300.0)
        while not stop.is_set():
            try:
                async with self._http() as client:
                    res = await self.adapter.discover(client)
                for url, body, text in res.raw_responses:
                    self.sink.reference(f"{self.venue}:rest@{self.health.run_id}", url, body, text)
                self.health.markets = len(res.markets)
                return res.markets
            except Exception as e:
                delay = backoff.next()
                log.warning("%s discovery failed (%s); retrying in %.0fs", self.venue, e, delay)
                self.sink.meta(None, "discovery_failed", {"error": f"{type(e).__name__}: {e}"[:500]})
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
        return None

    def apply_cap(self, subs: list[Subscription]) -> list[Subscription]:
        cap = self.vcfg.connection.max_total_subscriptions
        if len(subs) > cap:
            dropped = subs[cap:]
            self.health.subscriptions_dropped = len(dropped)
            self.sink.meta(
                None, "subscriptions_capped", {"cap": cap, "dropped": [s.key for s in dropped][:500]}
            )
            log.warning("%s: %d subscriptions over cap %d were dropped", self.venue, len(dropped), cap)
            return subs[:cap]
        return subs

    def start_shards(self, subs: list[Subscription], stop: asyncio.Event, prefix: str = "") -> None:
        for sid, items in shard_subscriptions(subs, self.vcfg.connection.max_subscriptions).items():
            shard_id = f"{prefix}{sid}"
            kwargs: dict[str, Any] = {}
            if self._connect is not None:
                kwargs["connect"] = self._connect
            shard = WsShard(
                adapter=self.adapter,
                shard_id=shard_id,
                subs=items,
                sink=self.sink,
                health=self.health,
                limiter=self.limiter,
                conn_cfg=self.vcfg.connection,
                run_id=self.health.run_id,
                **kwargs,
            )
            self.shards[shard_id] = shard
            self._shard_tasks[shard_id] = asyncio.create_task(shard.run(stop), name=f"shard-{shard_id}")
            self._planned_keys.update(s.key for s in items)

    async def refresh(self, stop: asyncio.Event) -> None:
        """Pick up newly listed markets without disturbing running connections."""
        markets = await self.discover(stop)
        if not markets:
            return
        plan = self.adapter.plan(markets)
        new = [s for s in plan if s.key not in self._planned_keys]
        room = self.vcfg.connection.max_total_subscriptions - len(self._planned_keys)
        if new:
            new = new[: max(0, room)]
        if new:
            self._next_extra += 1
            self.sink.meta(None, "plan_extended", {"added": [s.key for s in new]})
            self.start_shards(new, stop, prefix=f"r{self._next_extra}-")
        gone = self._planned_keys - {s.key for s in plan}
        if gone:
            self.sink.meta(None, "plan_markets_gone", {"keys": sorted(gone)[:500]})

    async def run(self, stop: asyncio.Event) -> None:
        self.sink.meta(None, "recorder_start", {"run_id": self.health.run_id, "pid": self.health.pid})
        markets = await self.discover(stop)
        if markets is None:
            return
        plan = self.apply_cap(self.adapter.plan(markets))
        self.health.subscriptions_planned = len(plan)
        self.sink.meta(
            None,
            "plan",
            {"markets": len(markets), "subscriptions": len(plan), "keys": [s.key for s in plan]},
        )
        log.info("%s: %d markets, %d subscriptions", self.venue, len(markets), len(plan))
        self.start_shards(plan, stop)
        refresh_every = self.vcfg.refresh_markets_s
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=refresh_every)
            if not stop.is_set():
                try:
                    await self.refresh(stop)
                except Exception as e:  # refresh problems must never take recording down
                    log.warning("%s refresh failed: %s", self.venue, e)
        await asyncio.gather(*self._shard_tasks.values(), return_exceptions=True)
        self.sink.meta(None, "recorder_stop", {"run_id": self.health.run_id})


async def housekeeping(
    archive: RawArchive,
    healths: dict[str, VenueHealth],
    health_dir: Path,
    stop: asyncio.Event,
    *,
    flush_interval_s: float,
    health_interval_s: float,
) -> None:
    """Flush quiet streams, close finished hours, publish health."""
    last_health = 0.0
    tick = max(0.2, min(flush_interval_s, health_interval_s, 5.0))
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=tick)
        archive.flush_all()
        archive.rotate_idle()
        now = time.monotonic()
        if now - last_health >= health_interval_s:
            for venue, h in healths.items():
                h.write(health_dir / f"{venue}.json")
            last_health = now


async def run_recorders(
    cfg: RecorderConfig,
    venues: list[str] | None = None,
    stop: asyncio.Event | None = None,
    *,
    duration_s: float | None = None,
    connect: Callable[..., Any] | None = None,
    http_client: Callable[[], httpx.AsyncClient] | None = None,
) -> dict[str, VenueHealth]:
    stop = stop or asyncio.Event()
    selected = [v for v in (venues or list(cfg.venues)) if cfg.venues[v].enabled]
    if not selected:
        raise ValueError("no enabled venues selected")
    run_id = secrets.token_hex(3)
    healths = {v: VenueHealth(venue=v, run_id=run_id) for v in selected}
    archive = make_archive(cfg, healths)
    locks = [VenueLock(Path(cfg.data_root), v) for v in selected]  # one recorder per venue
    recovered = archive.recover(selected)
    if recovered:
        log.warning("recovered %d unclosed files from a previous run", len(recovered))
    recorders = [
        VenueRecorder(cfg, cfg.venues[v], archive, healths[v], connect=connect, http_client=http_client)
        for v in selected
    ]
    for r in recorders:
        if recovered:
            r.sink.meta(
                None, "crash_recovery", {"files": [c.path.name for c in recovered if c.venue == r.venue]}
            )
    health_dir = Path(cfg.data_root) / "health"
    hk = asyncio.create_task(
        housekeeping(
            archive,
            healths,
            health_dir,
            stop,
            flush_interval_s=cfg.archive.flush_interval_s,
            health_interval_s=cfg.health_interval_s,
        )
    )
    timer = None
    if duration_s is not None:

        async def _timer() -> None:
            await asyncio.sleep(duration_s)
            stop.set()

        timer = asyncio.create_task(_timer())
    try:
        await asyncio.gather(*(r.run(stop) for r in recorders))
    finally:
        stop.set()
        if timer:
            timer.cancel()
        with contextlib.suppress(BaseException):
            await hk
        archive.close_all()
        for v, h in healths.items():
            h.write(health_dir / f"{v}.json")
        for lk in locks:
            lk.release()
    return healths
