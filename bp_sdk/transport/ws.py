"""bp_sdk.transport.ws — WebSocket transport for external agents.

Maintains one socket to the router; reconnects with jittered exponential
backoff. Hello/Welcome handshake on every (re)connect; offers
resume_token where applicable.

See `docs/router/protocol.md` §3 for the lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Optional

from bp_protocol import PROTOCOL_VERSION
from bp_protocol.frames import (
    ErrorFrame,
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
    `send()` queues into the active socket; on disconnect, frames sit in
    the outbox until the next connection drains them.
    """

    def __init__(self, config: "AgentConfig", *, info: "AgentInfo") -> None:
        self.config = config
        self.info = info
        self._inbox: asyncio.Queue[Frame] = asyncio.Queue()
        self._outbox: asyncio.Queue[Frame] = asyncio.Queue(
            maxsize=config.progress_buffer_size
        )
        self._connected = asyncio.Event()
        self._closed = asyncio.Event()
        self._welcome: Optional[WelcomeFrame] = None
        self._resume_token: Optional[str] = None
        self._loop_tasks: list[asyncio.Task] = []
        self._ws: Optional[object] = None

    @classmethod
    async def connect(
        cls, config: "AgentConfig", *, info: "AgentInfo"
    ) -> "WebSocketTransport":
        t = cls(config, info=info)
        await t._start()
        # Wait for the first successful handshake before returning.
        await t._connected.wait()
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
        for t in self._loop_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set() and not self._closed.is_set()

    @property
    def welcome(self) -> Optional[WelcomeFrame]:
        return self._welcome

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _start(self) -> None:
        self._loop_tasks.append(asyncio.create_task(self._connection_supervisor()))

    async def _connection_supervisor(self) -> None:
        backoff = self.config.reconnect_initial_backoff_s
        while not self._closed.is_set():
            try:
                await self._run_one_connection()
                # Clean exit (peer closed) — start a fresh backoff cycle.
                backoff = self.config.reconnect_initial_backoff_s
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ws_connection_failed",
                    extra={
                        "event": "ws_connection_failed",
                        "error": repr(exc),
                    },
                )

            if self._closed.is_set():
                return

            jitter = backoff * random.uniform(0.5, 1.5)
            try:
                await asyncio.wait_for(self._closed.wait(), timeout=jitter)
                return
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self.config.reconnect_max_backoff_s)

    async def _run_one_connection(self) -> None:
        """Open the socket, do handshake, run send/recv pumps until close."""
        import websockets  # noqa: PLC0415

        async with websockets.connect(
            self.config.router_url,
            max_size=2 * 1024 * 1024,
            ping_interval=None,  # we run our own heartbeat at protocol level
        ) as ws:
            self._ws = ws
            try:
                welcome = await self._do_hello(ws)
            except Exception:
                self._ws = None
                raise

            self._welcome = welcome
            self._resume_token = welcome.session_id
            self._connected.set()

            recv_task = asyncio.create_task(self._recv_pump(ws))
            send_task = asyncio.create_task(self._send_pump(ws))
            try:
                done, pending = await asyncio.wait(
                    [recv_task, send_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in done:
                    exc = t.exception()
                    if exc is not None:
                        raise exc
            finally:
                for t in (recv_task, send_task):
                    t.cancel()
                for t in (recv_task, send_task):
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                self._connected.clear()
                self._ws = None

    async def _do_hello(self, ws) -> WelcomeFrame:  # type: ignore[no-untyped-def]
        if not self.config.auth_token:
            raise RuntimeError(
                "WebSocketTransport.connect(): no auth_token in AgentConfig — "
                "run onboarding first via bp_sdk.onboarding.onboard_or_resume"
            )

        hello = HelloFrame(
            agent_id=self.info.agent_id,
            trace_id="0" * 32,
            span_id="0" * 16,
            auth_token=self.config.auth_token,
            sdk_version="0.1.0",
            agent_info=self.info,
            resume_token=self._resume_token,
        )
        await ws.send(serialize_frame(hello))
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        frame = parse_frame(raw)

        if isinstance(frame, ErrorFrame):
            raise RuntimeError(f"router rejected Hello: {frame.code}: {frame.message}")
        if not isinstance(frame, WelcomeFrame):
            raise RuntimeError(f"expected Welcome, got {frame.type}")
        if frame.protocol_version != PROTOCOL_VERSION:
            raise RuntimeError(
                f"router protocol_version {frame.protocol_version!r} mismatch"
            )
        logger.info(
            "ws_connected",
            extra={
                "event": "ws_connected",
                "bp.agent_id": self.info.agent_id,
                "session_id": frame.session_id,
            },
        )
        return frame

    async def _recv_pump(self, ws) -> None:  # type: ignore[no-untyped-def]
        async for raw in ws:
            try:
                frame = parse_frame(raw)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "frame_parse_failed",
                    extra={"event": "frame_parse_failed"},
                )
                continue
            await self._inbox.put(frame)

    async def _send_pump(self, ws) -> None:  # type: ignore[no-untyped-def]
        while True:
            frame = await self._outbox.get()
            await ws.send(serialize_frame(frame))
