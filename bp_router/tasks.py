"""bp_router.tasks — Task lifecycle helpers and background loops.

High-level operations on tasks:
- admit_task    — validate + ACL + create row + dispatch
- complete_task — persist Result + propagate to parent
- cancel_task   — recursive cancellation
- fail_task     — terminal-FAILED transition + result fan-out
- timeout_sweep_loop / file_gc_loop — background maintenance
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

from bp_protocol.frames import (
    AckFrame,
    CancelFrame,
    NewTaskFrame,
    ResultFrame,
)
from bp_protocol.types import TaskState, TaskStatus
from bp_router.acl.evaluator import Caller, Callee
from bp_router.db import queries
from bp_router.db.models import TaskRow
from bp_router.delivery import AgentNotConnected, deliver_frame
from bp_router.state import IllegalTransition, TaskNotFound, task_transition
from bp_router.visibility import caller_from_agent, callee_from_agent

if TYPE_CHECKING:
    from bp_router.app import AppState

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Errors surfaced to dispatch
# ---------------------------------------------------------------------------


class AdmitError(Exception):
    """Wraps an ACL/quota/validation refusal at admission time."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Admission (NewTask → tasks row → dispatch)
# ---------------------------------------------------------------------------


async def admit_task(
    state: "AppState",
    frame: NewTaskFrame,
    *,
    caller_agent_id: str,
) -> str:
    """Validate, ACL-check, persist, and dispatch a new task.

    Returns the assigned task_id. For idempotent retries (matching
    `idempotency_key`), the existing task_id is returned and no new
    work is scheduled.
    """
    pool = state.db_pool  # type: ignore[attr-defined]

    # 1. Idempotency lookup.
    if frame.idempotency_key:
        async with pool.acquire() as conn:
            existing = await queries.Scope.user(
                conn, frame.user_id
            ).find_idempotent(frame.idempotency_key)
        if existing is not None:
            return existing.task_id

    # 2. Look up caller and callee agent rows.
    async with pool.acquire() as conn:
        caller_row = await queries.get_agent(conn, caller_agent_id)
        callee_row = await queries.get_agent(conn, frame.destination_agent_id)
    if caller_row is None:
        raise AdmitError("agent_not_found", f"caller '{caller_agent_id}' unknown")
    if callee_row is None:
        raise AdmitError(
            "agent_not_found",
            f"destination '{frame.destination_agent_id}' unknown",
        )
    if callee_row.status != "active":
        raise AdmitError(
            "agent_not_found",
            f"destination '{frame.destination_agent_id}' not active",
        )

    # 3. ACL permission check (Caller carries session role/tier).
    role, user_tier = await _session_principals(state, frame.user_id)
    caller_view = caller_from_agent(caller_row, role=role, user_tier=user_tier)
    callee_view = callee_from_agent(callee_row)
    decision = state.acl.can_invoke(  # type: ignore[attr-defined]
        caller_view, callee_view, grants=frame.acl_grants
    )
    if not decision.allow:
        raise AdmitError(
            "acl_denied",
            f"caller '{caller_agent_id}' may not invoke "
            f"'{frame.destination_agent_id}' (rule={decision.rule_name})",
        )

    # 4. Persist task row + initial event.
    deadline = frame.deadline or (
        _now()
        + timedelta(seconds=state.settings.default_task_deadline_s)  # type: ignore[attr-defined]
    )
    async with pool.acquire() as conn:
        async with conn.transaction():
            task_row = await queries.Scope.user(conn, frame.user_id).create_task(
                session_id=frame.session_id,
                agent_id=frame.destination_agent_id,
                parent_task_id=frame.parent_task_id,
                priority=frame.priority,
                deadline=deadline,
                idempotency_key=frame.idempotency_key,
                input=frame.payload,
            )
            await queries.Scope.user(conn, frame.user_id).insert_task_event(
                task_id=task_row.task_id,
                kind="admitted",
                actor_agent_id=caller_agent_id,
                payload={"caller": caller_agent_id},
            )

    # 5. Dispatch the NewTask frame to the destination's socket.
    delivery_frame = NewTaskFrame(
        agent_id="router",
        trace_id=frame.trace_id,
        span_id=frame.span_id,
        task_id=task_row.task_id,
        parent_task_id=task_row.parent_task_id,
        destination_agent_id=frame.destination_agent_id,
        user_id=frame.user_id,
        session_id=frame.session_id,
        priority=frame.priority,
        deadline=task_row.deadline,
        payload=frame.payload,
        acl_grants=frame.acl_grants,
    )

    try:
        ack = await deliver_frame(
            state,
            frame.destination_agent_id,
            delivery_frame,
            await_ack=True,
            timeout_s=state.settings.pending_ack_timeout_s,  # type: ignore[attr-defined]
        )
    except AgentNotConnected:
        await fail_task(
            state,
            task_row.task_id,
            user_id=frame.user_id,
            status_code=503,
            reason="agent_disconnected",
            error={"code": "agent_disconnected"},
        )
        raise AdmitError("agent_disconnected", "destination agent has no live socket")
    except TimeoutError:
        await fail_task(
            state,
            task_row.task_id,
            user_id=frame.user_id,
            status_code=504,
            reason="ack_timeout",
            error={"code": "ack_timeout"},
        )
        raise AdmitError("ack_timeout", "destination agent did not ack in time")

    if ack is not None and not ack.accepted:
        await fail_task(
            state,
            task_row.task_id,
            user_id=frame.user_id,
            status_code=400,
            reason=ack.reason or "rejected",
            error={"code": "rejected", "reason": ack.reason},
        )
        raise AdmitError("rejected", ack.reason or "destination rejected the task")

    # 6. Transition QUEUED → RUNNING (the agent has accepted).
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                await task_transition(
                    conn,
                    task_row.task_id,
                    TaskState.RUNNING,
                    reason="agent_accepted",
                    actor_agent_id=frame.destination_agent_id,
                )
            except (IllegalTransition, TaskNotFound):
                # Result may already have arrived for very fast handlers;
                # tolerate.
                pass

    return task_row.task_id


# ---------------------------------------------------------------------------
# Completion / fan-out
# ---------------------------------------------------------------------------


async def complete_task(
    state: "AppState",
    frame: ResultFrame,
    *,
    reporting_agent_id: str,
) -> None:
    """Persist a Result and forward it to the parent agent."""
    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        # Look up task to find user_id + parent.
        row = await conn.fetchrow(
            "SELECT user_id, parent_task_id, agent_id FROM tasks WHERE task_id = $1",
            frame.task_id,
        )
        if row is None:
            logger.warning(
                "result_for_unknown_task",
                extra={"event": "result_for_unknown_task", "bp.task_id": frame.task_id},
            )
            return
        user_id = row["user_id"]
        parent_task_id = row["parent_task_id"]
        owning_agent_id = row["agent_id"]

        if reporting_agent_id != owning_agent_id:
            logger.warning(
                "result_from_wrong_agent",
                extra={
                    "event": "result_from_wrong_agent",
                    "bp.task_id": frame.task_id,
                    "expected": owning_agent_id,
                    "actual": reporting_agent_id,
                },
            )
            return

        new_state = _state_from_status(frame.status)
        async with conn.transaction():
            try:
                await task_transition(
                    conn,
                    frame.task_id,
                    new_state,
                    reason=f"result_{frame.status.value}",
                    actor_agent_id=reporting_agent_id,
                    status_code=frame.status_code,
                    output=(frame.output.model_dump() if frame.output else None),
                    error=frame.error,
                )
            except IllegalTransition:
                # Already terminal — drop the duplicate Result.
                logger.info(
                    "duplicate_result_dropped",
                    extra={
                        "event": "duplicate_result_dropped",
                        "bp.task_id": frame.task_id,
                    },
                )
                return

    # Fan out to parent agent (if any).
    if parent_task_id:
        async with pool.acquire() as conn:
            parent_row = await conn.fetchrow(
                "SELECT agent_id FROM tasks WHERE task_id = $1",
                parent_task_id,
            )
        if parent_row is not None:
            try:
                await deliver_frame(
                    state,
                    parent_row["agent_id"],
                    ResultFrame(
                        agent_id="router",
                        trace_id=frame.trace_id,
                        span_id=frame.span_id,
                        task_id=frame.task_id,
                        parent_task_id=parent_task_id,
                        status=frame.status,
                        status_code=frame.status_code,
                        output=frame.output,
                        error=frame.error,
                    ),
                    await_ack=False,
                )
            except AgentNotConnected:
                logger.info(
                    "parent_offline_result_dropped",
                    extra={
                        "event": "parent_offline_result_dropped",
                        "bp.task_id": frame.task_id,
                    },
                )


def _state_from_status(status: TaskStatus) -> TaskState:
    return {
        TaskStatus.SUCCEEDED: TaskState.SUCCEEDED,
        TaskStatus.FAILED: TaskState.FAILED,
        TaskStatus.CANCELLED: TaskState.CANCELLED,
        TaskStatus.TIMED_OUT: TaskState.TIMED_OUT,
    }[status]


# ---------------------------------------------------------------------------
# Cancellation (recursive)
# ---------------------------------------------------------------------------


async def cancel_task(
    state: "AppState",
    task_id: str,
    *,
    user_id: str,
    reason: str = "user_aborted",
    initiator: str = "user",
) -> int:
    """Cancel a task and all of its descendants."""
    pool = state.db_pool  # type: ignore[attr-defined]
    cancelled = 0

    async with pool.acquire() as conn:
        scope = queries.Scope.user(conn, user_id)
        descendants = await scope.list_descendants(task_id)
        targets = [task_id, *(d.task_id for d in descendants)]

    for tid in targets:
        async with pool.acquire() as conn:
            async with conn.transaction():
                try:
                    await task_transition(
                        conn,
                        tid,
                        TaskState.CANCELLED,
                        reason=reason,
                        actor_agent_id=initiator,
                    )
                    cancelled += 1
                except IllegalTransition:
                    continue
                except TaskNotFound:
                    continue

            owner = await conn.fetchrow(
                "SELECT agent_id, parent_task_id FROM tasks WHERE task_id = $1",
                tid,
            )
        if owner is None:
            continue
        try:
            await deliver_frame(
                state,
                owner["agent_id"],
                CancelFrame(
                    agent_id="router",
                    trace_id="0" * 32,
                    span_id="0" * 16,
                    task_id=tid,
                    reason=reason,
                ),
                await_ack=False,
            )
        except AgentNotConnected:
            pass

    return cancelled


# ---------------------------------------------------------------------------
# Failure helper
# ---------------------------------------------------------------------------


async def fail_task(
    state: "AppState",
    task_id: str,
    *,
    user_id: Optional[str] = None,
    status_code: int,
    reason: str,
    error: Optional[dict[str, Any]] = None,
    actor_agent_id: Optional[str] = None,
) -> None:
    """Transition a task to FAILED and propagate a Result to the parent."""
    pool = state.db_pool  # type: ignore[attr-defined]

    async with pool.acquire() as conn:
        if user_id is None:
            row = await conn.fetchrow(
                "SELECT user_id FROM tasks WHERE task_id = $1",
                task_id,
            )
            if row is None:
                return
            user_id = row["user_id"]

        async with conn.transaction():
            try:
                await task_transition(
                    conn,
                    task_id,
                    TaskState.FAILED,
                    reason=reason,
                    actor_agent_id=actor_agent_id,
                    status_code=status_code,
                    error=error or {"code": reason},
                )
            except IllegalTransition:
                return
            except TaskNotFound:
                return

        parent_row = await conn.fetchrow(
            "SELECT parent_task_id FROM tasks WHERE task_id = $1",
            task_id,
        )

    if parent_row is None or parent_row["parent_task_id"] is None:
        return

    async with pool.acquire() as conn:
        parent_owner = await conn.fetchrow(
            "SELECT agent_id FROM tasks WHERE task_id = $1",
            parent_row["parent_task_id"],
        )
    if parent_owner is None:
        return
    try:
        await deliver_frame(
            state,
            parent_owner["agent_id"],
            ResultFrame(
                agent_id="router",
                trace_id="0" * 32,
                span_id="0" * 16,
                task_id=task_id,
                parent_task_id=parent_row["parent_task_id"],
                status=TaskStatus.FAILED,
                status_code=status_code,
                error=error or {"code": reason},
            ),
            await_ack=False,
        )
    except AgentNotConnected:
        pass


# ---------------------------------------------------------------------------
# Disconnect cleanup
# ---------------------------------------------------------------------------


async def fail_inflight_for_agent(
    state: "AppState", agent_id: str, *, reason: str = "agent_disconnected"
) -> int:
    """Fail every non-terminal task currently assigned to `agent_id`.

    Called from ws_hub._on_disconnect when the resume window does not
    apply.
    """
    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT task_id FROM tasks
            WHERE agent_id = $1
              AND state IN ('QUEUED','RUNNING','WAITING_CHILDREN')
            """,
            agent_id,
        )
    failed = 0
    for r in rows:
        await fail_task(
            state,
            r["task_id"],
            status_code=503,
            reason=reason,
            error={"code": reason, "agent_id": agent_id},
            actor_agent_id="router",
        )
        failed += 1
    return failed


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------


async def timeout_sweep_loop(
    state: "AppState", *, interval_s: float = 5.0
) -> None:
    while True:
        try:
            await asyncio.sleep(interval_s)
            await _sweep_once(state)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception(
                "timeout_sweep_failed", extra={"event": "timeout_sweep_failed"}
            )


async def _sweep_once(state: "AppState") -> int:
    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        rows = await queries.find_expired_tasks(conn, now=_now(), limit=100)
    timed_out = 0
    for row in rows:
        await fail_task(
            state,
            row.task_id,
            user_id=row.user_id,
            status_code=504,
            reason="deadline_exceeded",
            error={"code": "deadline_exceeded"},
        )
        timed_out += 1
    return timed_out


async def file_gc_loop(state: "AppState", *, interval_s: float = 300.0) -> None:
    while True:
        try:
            await asyncio.sleep(interval_s)
            await _gc_files_once(state)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception(
                "file_gc_failed", extra={"event": "file_gc_failed"}
            )


async def _gc_files_once(state: "AppState") -> int:
    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        rows = await queries.find_expired_files(conn, now=_now(), limit=1000)
    deleted = 0
    for row in rows:
        try:
            await state.file_store.delete(row.sha256)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.exception(
                "file_delete_failed",
                extra={"event": "file_delete_failed", "file_id": row.file_id},
            )
            continue
        async with pool.acquire() as conn:
            await queries.delete_file_row(conn, row.file_id)
        deleted += 1
    return deleted


# ---------------------------------------------------------------------------
# Lifespan helper
# ---------------------------------------------------------------------------


async def start_background_loops(state: "AppState") -> list[asyncio.Task]:
    return [
        asyncio.create_task(timeout_sweep_loop(state)),
        asyncio.create_task(file_gc_loop(state)),
    ]


# ---------------------------------------------------------------------------
# Session / role lookup helper
# ---------------------------------------------------------------------------


async def _session_principals(
    state: "AppState", user_id: str
) -> tuple[Optional[str], Optional[str]]:
    """Return (role, user_tier) for the user. Cached at lookup time;
    callers can always re-fetch on quota changes.
    """
    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT role, user_tier FROM users WHERE user_id = $1", user_id
        )
    if row is None:
        return None, None
    return row["role"], row["user_tier"]
