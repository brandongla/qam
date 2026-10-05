"""Local fake venue servers for end-to-end recorder tests."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

import orjson
from websockets.asyncio.server import ServerConnection, serve


class FakeServer:
    """A websocket server running a per-connection handler; records what clients sent."""

    def __init__(self, handler: Callable[[FakeServer, ServerConnection, int], Awaitable[None]]) -> None:
        self.handler = handler
        self.received: list[tuple[int, Any]] = []  # (connection number, parsed message)
        self.connections = 0
        self.state: dict[str, Any] = {}
        self._server: Any = None
        self.port = 0

    async def __aenter__(self) -> FakeServer:
        async def _h(ws: ServerConnection) -> None:
            self.connections += 1
            n = self.connections
            with contextlib.suppress(Exception):
                await self.handler(self, ws, n)

        self._server = await serve(_h, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def recv_json(self, ws: ServerConnection, n: int, timeout: float = 5.0) -> Any:
        raw = await asyncio.wait_for(ws.recv(), timeout)
        msg = orjson.loads(raw)
        self.received.append((n, msg))
        return msg


async def hyperliquid_like(
    server: FakeServer, ws: ServerConnection, n: int, *, drop_first_after: int | None = None
) -> None:
    """Greets, acks subscriptions with one trade each, answers pings, keeps streaming bbo."""
    await ws.send("Websocket connection established.")
    sent = 0
    coins: list[str] = []

    async def stream_bbo() -> None:
        while True:
            await asyncio.sleep(0.05)
            for c in list(coins):
                await ws.send(
                    orjson.dumps({"channel": "bbo", "data": {"coin": c, "time": 1, "bbo": [None, None]}})
                )

    task = asyncio.create_task(stream_bbo())
    try:
        while True:
            msg = await server.recv_json(ws, n)
            if msg.get("method") == "ping":
                await ws.send('{"channel":"pong"}')
                continue
            sub = msg["subscription"]
            await ws.send(orjson.dumps({"channel": "subscriptionResponse", "data": msg}))
            if sub["type"] == "trades":
                await ws.send(orjson.dumps({"channel": "trades", "data": [{"coin": sub["coin"], "px": "1"}]}))
                sent += 1
            if sub["type"] == "bbo":
                coins.append(sub["coin"])
            if drop_first_after is not None and not server.state.get("dropped") and sent >= drop_first_after:
                server.state["dropped"] = n  # drop exactly one (main) connection, once
                await ws.close()
                return
    finally:
        task.cancel()


async def lighter_like(server: FakeServer, ws: ServerConnection, n: int) -> None:
    """Sends connected, pings the client, answers order_book subs with offsets incl. a regression."""
    await ws.send('{"type":"connected"}')
    await ws.send('{"type":"ping"}')
    while True:
        msg = await server.recv_json(ws, n)
        if msg.get("type") == "pong":
            continue
        ch = msg["channel"]
        name, _, ident = ch.partition("/")
        if name == "order_book":
            base = {"channel": f"order_book:{ident}", "order_book": {"asks": [], "bids": []}}
            await ws.send(orjson.dumps({**base, "type": "subscribed/order_book", "offset": 10}))
            await ws.send(orjson.dumps({**base, "type": "update/order_book", "offset": 12}))
            await ws.send(orjson.dumps({**base, "type": "update/order_book", "offset": 11}))  # regression
        elif name == "trade":
            await ws.send(orjson.dumps({"channel": f"trade:{ident}", "type": "update/trade", "trades": []}))
        elif name == "market_stats":
            await ws.send(orjson.dumps({"channel": "market_stats:all", "type": "update/market_stats"}))


async def silent(server: FakeServer, ws: ServerConnection, n: int) -> None:
    """Accepts and reads, but never sends data: should trigger stall detection."""
    while True:
        await server.recv_json(ws, n, timeout=60)
