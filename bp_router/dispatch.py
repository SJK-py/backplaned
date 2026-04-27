"""bp_router.dispatch — Frame-type → action dispatch.

The receive loop in `ws_hub` decodes one frame at a time and calls
`dispatch_frame`. Side effects are pushed to the right subsystem
(tasks, correlation, observability).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bp_protocol.frames import (
    AckFrame,
    CancelFrame,
    Frame,
    NewTaskFrame,
    PingFrame,
    PongFrame,
    ProgressFrame,
    ResultFrame,
)

if TYPE_CHECKING:
    from bp_router.app import AppState
    from bp_router.ws_hub import SocketEntry

logger = logging.getLogger(__name__)


async def dispatch_frame(
    state: "AppState",
    entry: "SocketEntry",
    frame: Frame,
) -> None:
    """Route an inbound frame to the right handler.

    The dispatcher does not own transactions or socket I/O — it
    delegates. Errors raised here propagate to the receive loop, which
    decides whether to send an Error frame or close the socket.
    """
    if isinstance(frame, NewTaskFrame):
        await _handle_new_task(state, entry, frame)
    elif isinstance(frame, ResultFrame):
        await _handle_result(state, entry, frame)
    elif isinstance(frame, ProgressFrame):
        await _handle_progress(state, entry, frame)
    elif isinstance(frame, CancelFrame):
        await _handle_cancel(state, entry, frame)
    elif isinstance(frame, AckFrame):
        await _handle_ack(state, entry, frame)
    elif isinstance(frame, PingFrame):
        await _handle_ping(state, entry, frame)
    elif isinstance(frame, PongFrame):
        await _handle_pong(state, entry, frame)
    else:
        logger.warning(
            "unexpected_frame_in_dispatch",
            extra={"event": "unexpected_frame_in_dispatch", "type": frame.type},
        )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_new_task(
    state: "AppState", entry: "SocketEntry", frame: NewTaskFrame
) -> None:
    """Validate, ACL-check, admit, dispatch.

    Mostly delegates to `bp_router.tasks.admit_task` — the wrapper here
    converts errors into appropriate Ack/Error responses on the socket.
    """
    raise NotImplementedError


async def _handle_result(
    state: "AppState", entry: "SocketEntry", frame: ResultFrame
) -> None:
    """Persist + propagate to parent. Send Ack to the reporting agent."""
    raise NotImplementedError


async def _handle_progress(
    state: "AppState", entry: "SocketEntry", frame: ProgressFrame
) -> None:
    """Fan-out: parent's socket + any UI subscribers. No persistence."""
    raise NotImplementedError


async def _handle_cancel(
    state: "AppState", entry: "SocketEntry", frame: CancelFrame
) -> None:
    """Recursive cancellation. See `bp_router.tasks.cancel_task`."""
    raise NotImplementedError


async def _handle_ack(
    state: "AppState", entry: "SocketEntry", frame: AckFrame
) -> None:
    """Resolve any pending ack for `frame.ref_correlation_id`."""
    state.correlation.resolve(frame.ref_correlation_id, frame)  # type: ignore[attr-defined]


async def _handle_ping(
    state: "AppState", entry: "SocketEntry", frame: PingFrame
) -> None:
    from bp_protocol.frames import PongFrame  # noqa: PLC0415

    pong = PongFrame(
        agent_id="router",
        trace_id=frame.trace_id,
        span_id=frame.span_id,
        ref_correlation_id=frame.correlation_id,
    )
    await entry.outbox.put(pong)


async def _handle_pong(
    state: "AppState", entry: "SocketEntry", frame: PongFrame
) -> None:
    state.correlation.resolve(frame.ref_correlation_id, frame)  # type: ignore[attr-defined]
