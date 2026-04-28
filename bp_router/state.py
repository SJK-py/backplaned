"""bp_router.state — Task state machine.

The single transition function (`task_transition`) is the only code path
that mutates `tasks.state`. CI lints for raw UPDATE statements that
bypass it.

See `docs/router/state.md` §1 for the spec.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from bp_protocol.types import TaskState

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Allowed transitions
# ---------------------------------------------------------------------------


_ALLOWED: dict[TaskState, set[TaskState]] = {
    TaskState.QUEUED: {
        TaskState.RUNNING,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.TIMED_OUT,
    },
    TaskState.RUNNING: {
        TaskState.WAITING_CHILDREN,
        TaskState.SUCCEEDED,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.TIMED_OUT,
    },
    TaskState.WAITING_CHILDREN: {
        TaskState.RUNNING,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.TIMED_OUT,
    },
    # Terminal states — no transitions out
    TaskState.SUCCEEDED: set(),
    TaskState.FAILED: set(),
    TaskState.CANCELLED: set(),
    TaskState.TIMED_OUT: set(),
}


class IllegalTransition(Exception):
    """Raised when a state transition violates the allowed table."""

    def __init__(self, task_id: str, frm: TaskState, to: TaskState) -> None:
        super().__init__(
            f"illegal transition for task {task_id}: {frm.value} → {to.value}"
        )
        self.task_id = task_id
        self.frm = frm
        self.to = to


class TaskNotFound(Exception):
    """Raised when the locked SELECT returns no row for the task_id."""


# ---------------------------------------------------------------------------
# Transition function
# ---------------------------------------------------------------------------


@dataclass
class TransitionResult:
    task_id: str
    previous_state: TaskState
    new_state: TaskState
    event_id: str


async def task_transition(
    conn: "asyncpg.Connection",
    task_id: str,
    new_state: TaskState,
    *,
    reason: str,
    actor_agent_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    status_code: Optional[int] = None,
    output: Optional[dict[str, Any]] = None,
    error: Optional[dict[str, Any]] = None,
) -> TransitionResult:
    """Transition a task atomically.

    1. Locks the task row (SELECT ... FOR UPDATE).
    2. Validates the transition against the static _ALLOWED table.
    3. Updates `tasks.state`, `updated_at`, and (when supplied)
       status_code / output / error.
    4. Inserts a row into `task_events` for audit.
    5. Emits a span event and a Prometheus counter increment.

    The caller MUST own an open transaction on `conn`. asyncpg's
    `async with conn.transaction(): ...` is the standard pattern.
    Concurrent transitions on the same task block on the row lock and
    re-validate, so two callers cannot both observe a non-terminal
    state and both transition out of it.
    """
    row = await conn.fetchrow(
        "SELECT user_id, state FROM tasks WHERE task_id = $1 FOR UPDATE",
        task_id,
    )
    if row is None:
        raise TaskNotFound(task_id)

    previous_state = TaskState(row["state"])

    if previous_state == new_state:
        # No-op; emit an event for observability but skip the UPDATE.
        event_id = await _insert_event(
            conn,
            task_id=task_id,
            kind="transition_noop",
            actor_agent_id=actor_agent_id,
            from_state=previous_state,
            to_state=new_state,
            payload={"reason": reason, **(payload or {})},
        )
        return TransitionResult(task_id, previous_state, new_state, event_id)

    if new_state not in _ALLOWED.get(previous_state, set()):
        raise IllegalTransition(task_id, previous_state, new_state)

    await conn.execute(
        """
        UPDATE tasks
        SET state = $2,
            status_code = COALESCE($3, status_code),
            output = COALESCE($4, output),
            error = COALESCE($5, error),
            updated_at = now()
        WHERE task_id = $1
        """,
        task_id,
        new_state.value,
        status_code,
        output,
        error,
    )

    event_id = await _insert_event(
        conn,
        task_id=task_id,
        kind="transition",
        actor_agent_id=actor_agent_id,
        from_state=previous_state,
        to_state=new_state,
        payload={"reason": reason, **(payload or {})},
    )

    # Best-effort metric increment + log line. Failures here must not
    # roll back the transition.
    try:
        from bp_router.observability.metrics import (  # noqa: PLC0415
            task_state_transitions_total,
        )

        task_state_transitions_total.labels(
            previous_state.value, new_state.value
        ).inc()
    except Exception:  # noqa: BLE001
        pass

    logger.info(
        "task_transitioned",
        extra={
            "event": "task_transitioned",
            "bp.task_id": task_id,
            "bp.state.from": previous_state.value,
            "bp.state.to": new_state.value,
            "reason": reason,
        },
    )

    return TransitionResult(task_id, previous_state, new_state, event_id)


async def _insert_event(
    conn: "asyncpg.Connection",
    *,
    task_id: str,
    kind: str,
    actor_agent_id: Optional[str],
    from_state: Optional[TaskState],
    to_state: Optional[TaskState],
    payload: dict[str, Any],
) -> str:
    row = await conn.fetchrow(
        """
        INSERT INTO task_events
            (task_id, ts, kind, actor_agent_id, from_state, to_state, payload)
        VALUES ($1, now(), $2, $3, $4, $5, $6)
        RETURNING event_id
        """,
        task_id,
        kind,
        actor_agent_id,
        from_state.value if from_state else None,
        to_state.value if to_state else None,
        payload,
    )
    return str(row["event_id"])


def is_allowed(frm: TaskState, to: TaskState) -> bool:
    """Pure helper: does the static table allow `frm → to`?"""
    return to in _ALLOWED.get(frm, set())


def allowed_transitions(frm: TaskState) -> frozenset[TaskState]:
    """Return the set of states reachable in one step from `frm`."""
    return frozenset(_ALLOWED.get(frm, set()))
