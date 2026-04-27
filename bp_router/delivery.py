"""bp_router.delivery — Helpers to push frames to live agent sockets.

Lives outside ws_hub.py so tasks.py can deliver without circular imports.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from bp_protocol.frames import AckFrame, Frame

if TYPE_CHECKING:
    from bp_router.app import AppState

logger = logging.getLogger(__name__)


class AgentNotConnected(Exception):
    """Raised when the destination agent has no live socket."""


async def deliver_frame(
    state: "AppState",
    agent_id: str,
    frame: Frame,
    *,
    await_ack: bool = True,
    timeout_s: Optional[float] = None,
) -> Optional[AckFrame]:
    """Push a frame to `agent_id`'s outbox and (optionally) await its ack.

    Returns the AckFrame if `await_ack=True`, else None. Raises
    `AgentNotConnected` if no live socket is registered, and re-raises
    `TimeoutError` if the ack doesn't arrive in time.
    """
    entry = state.socket_registry.get(agent_id)  # type: ignore[attr-defined]
    if entry is None:
        raise AgentNotConnected(agent_id)

    fut = None
    if await_ack:
        fut = state.correlation.register(  # type: ignore[attr-defined]
            frame.correlation_id,
            timeout_s=timeout_s,
        )
        entry.inflight_correlations.add(frame.correlation_id)

    try:
        entry.outbox.put_nowait(frame)
    except asyncio.QueueFull:
        # Backpressure — caller decides what to do. Drop the pending future
        # we just registered so we don't leak it.
        if fut is not None:
            state.correlation.reject(  # type: ignore[attr-defined]
                frame.correlation_id, RuntimeError("backpressure")
            )
            entry.inflight_correlations.discard(frame.correlation_id)
        raise

    if fut is None:
        return None

    try:
        ack = await fut
    finally:
        entry.inflight_correlations.discard(frame.correlation_id)
    return ack


def fanout_frame(
    state: "AppState",
    agent_ids: list[str],
    frame: Frame,
) -> int:
    """Best-effort delivery to many agents. No ack; drops on missing peer.

    Used for Progress fan-out (the canonical fire-and-forget path).
    Returns the number of sockets the frame was queued on.
    """
    delivered = 0
    for agent_id in agent_ids:
        entry = state.socket_registry.get(agent_id)  # type: ignore[attr-defined]
        if entry is None:
            continue
        try:
            entry.outbox.put_nowait(frame)
            delivered += 1
        except asyncio.QueueFull:
            logger.warning(
                "fanout_drop",
                extra={
                    "event": "fanout_drop",
                    "bp.agent_id": agent_id,
                    "bp.frame.type": frame.type,
                },
            )
    return delivered
