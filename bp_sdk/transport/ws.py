"""bp_sdk.transport.ws — WebSocket transport for external agents.

Maintains one socket to the router; reconnects with jittered exponential
backoff. Hello/Welcome handshake on every (re)connect; offers
resume_token where applicable.

See `docs/design/router/protocol.md` §3 for the lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Optional

from bp_protocol.frames import (
    Frame,
    HelloFrame,
    WelcomeFrame,
    parse_frame,
    serialize_frame,
)

if TYPE_CHECKING:
    from bp_protocol.types import AgentInfo
    from bp_sdk.settings import AgentConfig

logger = logging.getLogger(__name__)


class WebSocketTransport:
    """One-socket-per-agent transport.

    `recv()` blocks until a frame is available; reconnect is transparent.
    `send()` queues into the active socket; on disconnect, the queue is
    flushed before the failover.
    """

    def __init__(self, config: "AgentConfig", *, info: "AgentInfo") -> None:
        self.config = config
        self.info = info
        self._ws: Optional[object] = None  # websockets client; deferred import
        self._inbox: asyncio.Queue[Frame] = asyncio.Queue()
        self._outbox: asyncio.Queue[Frame] = asyncio.Queue()
        self._connected = asyncio.Event()
        self._closed = asyncio.Event()
        self._welcome: Optional[WelcomeFrame] = None
        self._resume_token: Optional[str] = None
        self._loop_tasks: list[asyncio.Task] = []

    @classmethod
    async def connect(
        cls, config: "AgentConfig", *, info: "AgentInfo"
    ) -> "WebSocketTransport":
        t = cls(config, info=info)
        await t._start()
        return t

    # ------------------------------------------------------------------
    # Public Transport surface
    # ------------------------------------------------------------------

    async def send(self, frame: Frame) -> None:
        await self._outbox.put(frame)

    async def recv(self) -> Frame:
        return await self._inbox.get()

    async def close(self) -> None:
        self._closed.set()
        for t in self._loop_tasks:
            t.cancel()
        # Implementation: drain outbox with deadline, send graceful close.

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def welcome(self) -> Optional[WelcomeFrame]:
        return self._welcome

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _start(self) -> None:
        self._loop_tasks.append(asyncio.create_task(self._connection_supervisor()))

    async def _connection_supervisor(self) -> None:
        """Connect, run, on_disconnect, backoff, repeat — until closed."""
        backoff = self.config.reconnect_initial_backoff_s
        while not self._closed.is_set():
            try:
                await self._run_one_connection()
                backoff = self.config.reconnect_initial_backoff_s
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception(
                    "ws_connection_failed",
                    extra={"event": "ws_connection_failed"},
                )

            if self._closed.is_set():
                return

            jitter = backoff * random.uniform(0.5, 1.5)
            await asyncio.sleep(jitter)
            backoff = min(backoff * 2, self.config.reconnect_max_backoff_s)

    async def _run_one_connection(self) -> None:
        """Open the socket, do handshake, run send/recv pumps until close."""
        # Implementation: import `websockets` lazily, connect to
        # config.router_url, send HelloFrame, await WelcomeFrame, then
        # run two pumps in parallel until either fails.
        raise NotImplementedError

    async def _do_hello(self) -> WelcomeFrame:
        hello = HelloFrame(
            agent_id=self.info.agent_id,
            trace_id="0" * 32,
            span_id="0" * 16,
            auth_token=self.config.auth_token or "",
            sdk_version="0.1.0",
            agent_info=self.info,
            resume_token=self._resume_token,
        )
        # await self._ws.send(serialize_frame(hello))
        # raw = await self._ws.recv()
        # frame = parse_frame(raw)
        raise NotImplementedError
