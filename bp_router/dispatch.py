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
    ErrorCode,
    ErrorFrame,
    Frame,
    NewTaskFrame,
    PingFrame,
    PongFrame,
    ProgressFrame,
    ResultFrame,
)
from bp_router.delivery import AgentNotConnected, deliver_frame, fanout_frame

if TYPE_CHECKING:
    from bp_router.app import AppState
    from bp_router.ws_hub import SocketEntry

logger = logging.getLogger(__name__)


async def dispatch_frame(
    state: "AppState",
    entry: "SocketEntry",
    frame: Frame,
) -> None:
    """Route an inbound frame to the right handler."""
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
    """Validate, ACL-check, admit, dispatch."""
    from bp_router.tasks import AdmitError, admit_task  # noqa: PLC0415

    try:
        task_id = await admit_task(state, frame, caller_agent_id=entry.agent_id)
    except AdmitError as exc:
        ack = AckFrame(
            agent_id="router",
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            ref_correlation_id=frame.correlation_id,
            accepted=False,
            reason=exc.message,
        )
        await entry.outbox.put(ack)
        return
    except Exception:  # noqa: BLE001
        logger.exception(
            "admit_task_failed",
            extra={"event": "admit_task_failed"},
        )
        ack = AckFrame(
            agent_id="router",
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            ref_correlation_id=frame.correlation_id,
            accepted=False,
            reason="internal_error",
        )
        await entry.outbox.put(ack)
        return

    # Acknowledge the spawn with the assigned task_id.
    ack = AckFrame(
        agent_id="router",
        trace_id=frame.trace_id,
        span_id=frame.span_id,
        ref_correlation_id=frame.correlation_id,
        accepted=True,
        task_id=task_id,
    )
    await entry.outbox.put(ack)


async def _handle_result(
    state: "AppState", entry: "SocketEntry", frame: ResultFrame
) -> None:
    """Persist + propagate to parent. Send Ack to the reporting agent."""
    from bp_router.tasks import complete_task  # noqa: PLC0415

    try:
        await complete_task(state, frame, reporting_agent_id=entry.agent_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "complete_task_failed",
            extra={"event": "complete_task_failed", "bp.task_id": frame.task_id},
        )
        ack = AckFrame(
            agent_id="router",
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            ref_correlation_id=frame.correlation_id,
            accepted=False,
            reason="internal_error",
        )
        await entry.outbox.put(ack)
        return

    ack = AckFrame(
        agent_id="router",
        trace_id=frame.trace_id,
        span_id=frame.span_id,
        ref_correlation_id=frame.correlation_id,
        accepted=True,
    )
    await entry.outbox.put(ack)


async def _handle_progress(
    state: "AppState", entry: "SocketEntry", frame: ProgressFrame
) -> None:
    """Fan-out to the parent agent's socket. No persistence; best-effort."""
    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT t.parent_task_id, parent.agent_id AS parent_agent_id
            FROM tasks t
            LEFT JOIN tasks parent ON parent.task_id = t.parent_task_id
            WHERE t.task_id = $1
            """,
            frame.task_id,
        )
    if row is None or row["parent_agent_id"] is None:
        return
    fanout_frame(state, [row["parent_agent_id"]], frame)


async def _handle_cancel(
    state: "AppState", entry: "SocketEntry", frame: CancelFrame
) -> None:
    """Recursive cancellation initiated by an agent."""
    from bp_router.tasks import cancel_task  # noqa: PLC0415

    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM tasks WHERE task_id = $1",
            frame.task_id,
        )
    if row is None:
        return

    await cancel_task(
        state,
        frame.task_id,
        user_id=row["user_id"],
        reason=frame.reason,
        initiator=entry.agent_id,
    )


async def _handle_ack(
    state: "AppState", entry: "SocketEntry", frame: AckFrame
) -> None:
    """Resolve any pending ack for `frame.ref_correlation_id`."""
    state.correlation.resolve(frame.ref_correlation_id, frame)  # type: ignore[attr-defined]


async def _handle_ping(
    state: "AppState", entry: "SocketEntry", frame: PingFrame
) -> None:
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
