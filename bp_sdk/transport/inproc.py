"""bp_sdk.transport.inproc — In-process transport for embedded agents.

Frames are passed through asyncio queues to the router's dispatch loop
in the same process. No serialization, no network. The router's
embedded-agent registry constructs paired queues at startup.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from bp_protocol.frames import Frame

if TYPE_CHECKING:
    from bp_protocol.types import AgentInfo
    from bp_sdk.settings import AgentConfig


class InProcessTransport:
    """Asyncio-queue-backed transport.

    Construction is two-step: the router-side embedded-agent registry
    creates the paired queues and hands them to this transport via
    `attach`. `connect()` then waits for attachment to complete before
    returning.
    """

    def __init__(self) -> None:
        self._inbound: asyncio.Queue[Frame] = asyncio.Queue()
        """Frames the router sends to this agent."""

        self._outbound: asyncio.Queue[Frame] = asyncio.Queue()
        """Frames this agent sends to the router."""

        self._closed = asyncio.Event()
        self._attached = asyncio.Event()

    @classmethod
    async def connect(
        cls, config: "AgentConfig", *, info: "AgentInfo"
    ) -> "InProcessTransport":
        """Construct an InProcessTransport.

        For embedded agents, the router-side `attach_embedded_agent`
        is expected to call `attach()` on this instance with the
        paired queues. `connect()` returns immediately when called
        from the router's lifespan; the attach is what actually
        wires the queues. Callers from outside the router should
        prefer `WebSocketTransport`.
        """
        return cls()

    # ------------------------------------------------------------------
    # Router-side hooks
    # ------------------------------------------------------------------

    def attach(
        self,
        *,
        inbound: asyncio.Queue[Frame],
        outbound: asyncio.Queue[Frame],
    ) -> None:
        """Replace the queues with the router-side ones and signal ready."""
        self._inbound = inbound
        self._outbound = outbound
        self._attached.set()

    def router_inbox(self) -> asyncio.Queue[Frame]:
        """Queue the router reads to receive frames from this agent."""
        return self._outbound

    def router_outbox(self) -> asyncio.Queue[Frame]:
        """Queue the router writes to deliver frames to this agent."""
        return self._inbound

    # ------------------------------------------------------------------
    # Transport surface
    # ------------------------------------------------------------------

    async def send(self, frame: Frame) -> None:
        # Wait for attach in case the agent boots before the router
        # finishes wiring the queues.
        if not self._attached.is_set():
            await self._attached.wait()
        await self._outbound.put(frame)

    async def recv(self) -> Frame:
        if not self._attached.is_set():
            await self._attached.wait()
        return await self._inbound.get()

    async def close(self) -> None:
        self._closed.set()

    @property
    def is_connected(self) -> bool:
        return self._attached.is_set() and not self._closed.is_set()

    @property
    def welcome(self):  # type: ignore[no-untyped-def]
        """Embedded agents don't go through Hello/Welcome; we don't
        publish a catalog through this channel. Returns None so callers
        that read transport.welcome stay safe."""
        return None
