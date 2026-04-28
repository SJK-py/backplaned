"""bp_router.embedded — Registry for in-process embedded agents.

Embedded agents share the router's asyncio event loop. They are paired
with InProcessTransport instances via this registry so the router's
delivery path (`bp_router.delivery.deliver_frame`) can route frames to
either a live WebSocket or an in-process queue uniformly.

Embedded registration is conventionally driven from deployment config
(router.embedded_agents = ["my_agents.echo:agent", ...]) via the
lifespan startup. The Phase B/C wiring leaves the registration call
explicit; future work introduces the config-driven loader.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from bp_protocol.frames import Frame
from bp_router.ws_hub import SocketEntry

if TYPE_CHECKING:
    from bp_router.app import AppState
    from bp_sdk.agent import Agent

logger = logging.getLogger(__name__)


async def attach_embedded_agent(state: "AppState", agent: "Agent") -> SocketEntry:
    """Register an embedded `Agent` with the router and return the
    SocketEntry the router uses to deliver frames into it.

    Wires:
      1. Two asyncio.Queues — router→agent and agent→router.
      2. A SocketEntry whose `outbox` is the router→agent queue, so
         deliver_frame() works without modification.
      3. The agent's InProcessTransport so its `recv` reads from
         router→agent and `send` writes into agent→router.
      4. A pump that drains agent→router into the router-side dispatch
         loop (dispatch_frame), passing the SocketEntry as the
         attribution context.
    """
    from bp_router.dispatch import dispatch_frame  # noqa: PLC0415
    from bp_sdk.transport.inproc import InProcessTransport  # noqa: PLC0415

    # The transport may already exist (created by Agent.__init__) — only
    # the embedded path needs it. We always allocate a fresh one here so
    # callers don't have to coordinate.
    transport: InProcessTransport = (
        agent._dispatcher.transport if agent._dispatcher else InProcessTransport()  # type: ignore[attr-defined]
    )

    inbound: asyncio.Queue[Frame] = asyncio.Queue()  # router → agent
    outbound: asyncio.Queue[Frame] = asyncio.Queue()  # agent → router

    transport.attach(inbound=inbound, outbound=outbound)

    entry = SocketEntry(
        agent_id=agent.info.agent_id,
        websocket=None,  # type: ignore[arg-type]
        session_token="",
        outbox=inbound,
    )

    # Supersede any existing socket for this agent_id.
    previous = await state.socket_registry.attach(entry)  # type: ignore[attr-defined]
    if previous is not None:
        previous.closed.set()

    # Pump agent→router → dispatch_frame.
    async def _pump() -> None:
        while not entry.closed.is_set():
            try:
                frame = await outbound.get()
            except asyncio.CancelledError:
                return
            try:
                await dispatch_frame(state, entry, frame)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "embedded_dispatch_failed",
                    extra={"event": "embedded_dispatch_failed"},
                )

    from bp_router.app import spawn_background  # noqa: PLC0415

    spawn_background(state, _pump())

    logger.info(
        "embedded_agent_attached",
        extra={"event": "embedded_agent_attached", "bp.agent_id": agent.info.agent_id},
    )
    return entry
