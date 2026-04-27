"""bp_router.state — Task state machine.

The single transition function (`task_transition`) is the only code path
that mutates `tasks.state`. CI lints for raw UPDATE statements that
bypass it.

See `docs/design/router/state.md` §1 for the spec.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from bp_protocol.types import TaskState

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
    conn: Any,  # asyncpg.Connection
    task_id: str,
    new_state: TaskState,
    *,
    reason: str,
    actor_agent_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> TransitionResult:
    """Transition a task atomically.

    1. Locks the task row (SELECT ... FOR UPDATE).
    2. Validates the transition.
    3. Updates `tasks.state` and `tasks.updated_at`.
    4. Inserts a row into `task_events` (audit log).
    5. Emits OTel span event + Prometheus counter increment.

    Caller is responsible for managing the surrounding transaction.
    """
    # NOTE: Implementation pending — see bp_router.db.queries for query
    # builders. This signature is the stable contract.
    raise NotImplementedError


def is_allowed(frm: TaskState, to: TaskState) -> bool:
    """Pure helper: does the static table allow `frm → to`?"""
    return to in _ALLOWED.get(frm, set())


def allowed_transitions(frm: TaskState) -> frozenset[TaskState]:
    """Return the set of states reachable in one step from `frm`."""
    return frozenset(_ALLOWED.get(frm, set()))
