"""bp_router.tasks — Task lifecycle helpers and background loops.

Houses the high-level operations on tasks:
- admit (validate + create DB row + dispatch)
- complete (terminal Result + propagate to parent)
- cancel (recursive cancel + frame fan-out)
- timeout sweep (background loop)
- file GC (background loop)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Optional

from bp_protocol.frames import NewTaskFrame, ResultFrame
from bp_protocol.types import TaskState, TaskStatus

if TYPE_CHECKING:
    from bp_router.app import AppState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Admission (NewTask → tasks row)
# ---------------------------------------------------------------------------


async def admit_task(
    state: "AppState",
    frame: NewTaskFrame,
    *,
    caller_agent_id: str,
) -> str:
    """Validate and create a `tasks` row for a NewTask frame.

    Steps (see `docs/design/router/state.md` §1.3 + `acl.md` §6):
      1. Idempotency-key check (return existing task_id if seen).
      2. Validate `payload` against destination's `accepts_schema`.
      3. ACL evaluate (caller, callee, "permission") — including grants.
      4. Quota check (tasks_started, tasks_concurrent, ...).
      5. Insert tasks row (state=QUEUED).
      6. Enqueue dispatch.

    Returns the assigned `task_id`.
    Raises typed errors for ACL deny, quota exceeded, schema mismatch.
    """
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Completion / fan-out
# ---------------------------------------------------------------------------


async def complete_task(
    state: "AppState",
    frame: ResultFrame,
    *,
    reporting_agent_id: str,
) -> None:
    """Persist a Result and forward it to the parent agent.

    Persists before fan-out so a router crash mid-fan-out does not
    lose the result. See `docs/design/router/protocol.md` §4.2.
    """
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Cancellation (recursive)
# ---------------------------------------------------------------------------


async def cancel_task(
    state: "AppState",
    task_id: str,
    *,
    reason: str = "user_aborted",
    initiator: str = "user",
) -> int:
    """Cancel a task and all of its descendants.

    Returns the number of tasks transitioned. Idempotent — already-
    cancelled tasks are skipped.
    """
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------


async def timeout_sweep_loop(state: "AppState", *, interval_s: float = 5.0) -> None:
    """Periodically scan for tasks past their deadline and time them out.

    Uses the partial index `tasks(state) WHERE state IN
    ('QUEUED','RUNNING','WAITING_CHILDREN')` for cheap scans.
    """
    while True:
        try:
            await asyncio.sleep(interval_s)
            await _sweep_once(state)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception("timeout_sweep_failed", extra={"event": "timeout_sweep_failed"})


async def _sweep_once(state: "AppState") -> int:
    """One pass of the timeout sweep. Returns count of tasks timed out."""
    raise NotImplementedError


async def file_gc_loop(state: "AppState", *, interval_s: float = 300.0) -> None:
    """Periodically delete expired files from storage and the DB."""
    while True:
        try:
            await asyncio.sleep(interval_s)
            await _gc_files_once(state)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception("file_gc_failed", extra={"event": "file_gc_failed"})


async def _gc_files_once(state: "AppState") -> int:
    """One pass of the file GC. Returns count of files deleted."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Helpers exposed to the WS dispatcher
# ---------------------------------------------------------------------------


async def fail_task(
    state: "AppState",
    task_id: str,
    *,
    status_code: int,
    reason: str,
    error: Optional[dict[str, Any]] = None,
) -> None:
    """Transition a task to FAILED and propagate a Result frame upward."""
    raise NotImplementedError


async def start_background_loops(state: "AppState") -> list[asyncio.Task]:
    """Kick off long-running router-side loops. Returns task handles for
    the lifespan to cancel on shutdown."""
    return [
        asyncio.create_task(timeout_sweep_loop(state)),
        asyncio.create_task(file_gc_loop(state)),
    ]
