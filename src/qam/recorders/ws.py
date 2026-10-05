"""One websocket connection ("shard") with reconnect, keepalive, stall detection and pacing."""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Callable
from typing import Any

from websockets.asyncio.client import connect as ws_connect

from qam.recorders.config import ConnectionConfig
from qam.recorders.health import VenueHealth
from qam.recorders.sink import RecorderSink
from qam.recorders.venues.base import Subscription, VenueAdapter


class RateLimiter:
    """Venue-wide token bucket for outgoing subscribe messages."""

    def __init__(self, rate_per_s: float, burst: int | None = None) -> None:
        self.rate = max(rate_per_s, 0.001)
        self.capacity = float(burst if burst is not None else max(1, int(rate_per_s)))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class Backoff:
    """Exponential backoff with full jitter."""

    def __init__(self, initial: float, maximum: float, rng: random.Random | None = None) -> None:
        self.initial = initial
        self.maximum = maximum
        self.attempt = 0
        self.rng = rng or random.Random()

    def next(self) -> float:
        cap = min(self.maximum, self.initial * (2**self.attempt))
        self.attempt += 1
        return self.rng.uniform(cap / 2, cap)

    def reset(self) -> None:
        self.attempt = 0


class StallError(Exception):
    pass


class WsShard:
    """Runs one connection forever (until ``stop`` is set), recording everything it receives."""

    #: a connection that delivered data for at least this long resets the backoff
    STABLE_AFTER_S = 60.0
    #: if the venue never signals readiness, subscribe anyway after this long
    READY_TIMEOUT_S = 5.0

    def __init__(
        self,
        *,
        adapter: VenueAdapter,
        shard_id: str,
        subs: list[Subscription],
        sink: RecorderSink,
        health: VenueHealth,
        limiter: RateLimiter,
        conn_cfg: ConnectionConfig,
        run_id: str,
        connect: Callable[..., Any] = ws_connect,
    ) -> None:
        self.adapter = adapter
        self.shard_id = shard_id
        self.subs = list(subs)
        self.sink = sink
        self.health = health
        self.limiter = limiter
        self.cfg = conn_cfg
        self.run_id = run_id
        self._connect = connect
        self.epoch = 0
        self.alias = {s.classify_as: s.stream for s in subs if s.classify_as}
        self.backoff = Backoff(conn_cfg.backoff_initial_s, conn_cfg.backoff_max_s)

    @property
    def venue(self) -> str:
        return self.adapter.name

    def conn_id(self) -> str:
        return f"{self.venue}:{self.shard_id}#{self.epoch}@{self.run_id}"

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.epoch += 1
            reason = await self._session(stop)
            if stop.is_set():
                break
            self.health.note_reconnect(reason)
            delay = self.backoff.next()
            self.sink.meta(self.conn_id(), "reconnect_wait", {"delay_s": round(delay, 3), "reason": reason})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    async def _session(self, stop: asyncio.Event) -> str:
        """One connection lifetime. Returns the reason it ended."""
        conn = self.conn_id()
        cs = self.health.conn(self.shard_id)
        cs.state, cs.subs, cs.last_error = "connecting", len(self.subs), None
        self.sink.meta(
            conn, "connecting", {"url": self.adapter.ws_url, "shard": self.shard_id, "subs": len(self.subs)}
        )
        tasks: list[asyncio.Task[Any]] = []
        try:
            async with self._connect(
                self.adapter.ws_url,
                open_timeout=self.cfg.open_timeout_s,
                ping_interval=self.cfg.ping_interval_s,
                ping_timeout=self.cfg.ping_timeout_s,
                max_size=self.cfg.max_message_bytes,
                close_timeout=5,
            ) as ws:
                cs.state = "open"
                cs.connected_since_ns = time.time_ns()
                self.sink.meta(conn, "connected", {"shard": self.shard_id})
                ready = asyncio.Event()
                if self.adapter.subscribe_on_open:
                    ready.set()
                tasks.append(asyncio.create_task(self._subscribe_when_ready(ws, ready, conn)))
                if self.adapter.keepalive_interval_s:
                    tasks.append(asyncio.create_task(self._keepalive(ws)))
                stop_task = asyncio.create_task(stop.wait())
                recv_task = asyncio.create_task(self._recv_loop(ws, conn, ready))
                tasks += [stop_task, recv_task]
                done, _ = await asyncio.wait({stop_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
                if stop_task in done:
                    self.sink.meta(conn, "closing", {"reason": "shutdown"})
                    return "shutdown"
                exc = recv_task.exception()
                raise exc if exc else ConnectionError("receive loop ended")
        except StallError as e:
            reason = f"stall: {e}"
        except Exception as e:  # network errors, protocol errors, server closes
            reason = f"{type(e).__name__}: {e}"[:300]
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(BaseException):
                    await t
            cs.state = "closed"
        cs.last_error = reason
        self.sink.meta(conn, "disconnected", {"reason": reason})
        return reason

    async def _subscribe_when_ready(self, ws: Any, ready: asyncio.Event, conn: str) -> None:
        try:
            await asyncio.wait_for(ready.wait(), timeout=self.READY_TIMEOUT_S)
            note = None
        except TimeoutError:
            note = "no ready signal; subscribing after timeout"
        self.sink.meta(conn, "subscribing", {"keys": [s.key for s in self.subs], "note": note})
        for s in self.subs:
            await self.limiter.acquire()
            await ws.send(s.message)
        cs = self.health.conn(self.shard_id)
        cs.state = "subscribed"
        self.sink.meta(conn, "subscribed", {"count": len(self.subs)})

    async def _keepalive(self, ws: Any) -> None:
        interval = float(self.adapter.keepalive_interval_s or 0)
        msg = self.adapter.keepalive_message()
        if not interval or msg is None:
            return
        while True:
            await asyncio.sleep(interval)
            await ws.send(msg)

    async def _recv_loop(self, ws: Any, conn: str, ready: asyncio.Event) -> None:
        cs = self.health.conn(self.shard_id)
        seq = 0
        last_data = time.monotonic()
        connected_at = last_data
        last_seq: dict[str, int] = {}
        stall = self.cfg.stall_timeout_s
        while True:
            timeout = stall - (time.monotonic() - last_data)
            if timeout <= 0:
                raise StallError(f"no data for {stall:.0f}s")
            try:
                frame = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except TimeoutError:
                raise StallError(f"no data for {stall:.0f}s") from None
            recv_ns = time.time_ns()
            mono_ns = time.monotonic_ns()
            extra = None
            if isinstance(frame, bytes):
                text = frame.decode("utf-8", "replace")
                extra = {"bin": True}
            else:
                text = frame
            seq += 1
            c = self.adapter.classify(text)
            stream = self.alias.get(c.stream, c.stream)
            self.sink.message(
                conn=conn,
                seq=seq,
                raw=text,
                stream=stream,
                channel=c.channel,
                market=c.market,
                recv_ns=recv_ns,
                mono_ns=mono_ns,
                extra=extra,
            )
            cs.msgs += 1
            cs.last_msg_ns = recv_ns
            if c.reply is not None:
                await ws.send(c.reply)
            if c.ready:
                ready.set()
            if c.error is not None:
                self.health.venue_errors += 1
                self.sink.meta(conn, "venue_error", {"error": c.error[:1000]})
            if c.stream == "unknown":
                self.health.unknown_msgs += 1
            if c.channel == "shutdown":
                self.sink.meta(conn, "server_shutdown_notice", {"raw": text[:500]})
            if c.seq is not None:
                key, val = c.seq
                prev = last_seq.get(key)
                if prev is not None and val < prev:
                    self.health.seq_regressions += 1
                    self.sink.meta(conn, "seq_regression", {"key": key, "prev": prev, "value": val})
                last_seq[key] = val
            if c.is_data:
                now = time.monotonic()
                last_data = now
                cs.data_msgs += 1
                cs.last_data_ns = recv_ns
                if self.backoff.attempt and now - connected_at >= self.STABLE_AFTER_S:
                    self.backoff.reset()
