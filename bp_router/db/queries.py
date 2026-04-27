"""bp_router.db.queries — Query helpers with the user_id scoping invariant.

EVERY read of a user-owned table goes through `Scope.user(user_id)`. The
returned wrapper enforces `WHERE user_id = $1` on its method's queries.
CI greps for `SELECT|UPDATE|DELETE` patterns in this module that are not
behind `Scope` to catch invariant violations.

See `docs/design/security.md` §8 for the data isolation rationale.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from bp_protocol.types import TaskPriority, TaskState
from bp_router.db.models import (
    AgentRow,
    FileRow,
    SessionRow,
    TaskEventRow,
    TaskRow,
    UserRow,
)

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str = "") -> str:
    return f"{prefix}{secrets.token_urlsafe(16)}"


# ---------------------------------------------------------------------------
# Scope wrapper — enforces WHERE user_id = $current_user
# ---------------------------------------------------------------------------


class Scope:
    """Data-access wrapper bound to a single user_id.

    Use as `await Scope.user(conn, user_id).get_session(session_id)`. The
    wrapper's queries always include `user_id = $X` in the WHERE clause.
    Callers that must read across users (admin endpoints) use `Scope.admin()`.
    """

    def __init__(self, conn: "asyncpg.Connection", user_id: Optional[str]) -> None:
        self._conn = conn
        self._user_id = user_id

    @classmethod
    def user(cls, conn: "asyncpg.Connection", user_id: str) -> "Scope":
        return cls(conn, user_id)

    @classmethod
    def admin(cls, conn: "asyncpg.Connection") -> "Scope":
        """Cross-user reads. Reserved for endpoints under `admin` role."""
        return cls(conn, None)

    @property
    def is_admin(self) -> bool:
        return self._user_id is None

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def open_session(self, *, metadata: Optional[dict[str, Any]] = None) -> SessionRow:
        raise NotImplementedError

    async def close_session(self, session_id: str) -> None:
        raise NotImplementedError

    async def get_session(self, session_id: str) -> Optional[SessionRow]:
        raise NotImplementedError

    async def list_sessions(self, *, limit: int = 50) -> list[SessionRow]:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    async def create_task(
        self,
        *,
        session_id: str,
        agent_id: str,
        parent_task_id: Optional[str],
        priority: TaskPriority,
        deadline: Optional[datetime],
        idempotency_key: Optional[str],
        input: dict[str, Any],
    ) -> TaskRow:
        raise NotImplementedError

    async def get_task(self, task_id: str) -> Optional[TaskRow]:
        raise NotImplementedError

    async def get_task_for_update(self, task_id: str) -> Optional[TaskRow]:
        """SELECT ... FOR UPDATE — used by `task_transition`."""
        raise NotImplementedError

    async def list_session_tasks(
        self, session_id: str, *, limit: int = 100
    ) -> list[TaskRow]:
        raise NotImplementedError

    async def find_idempotent(
        self, idempotency_key: str
    ) -> Optional[TaskRow]:
        raise NotImplementedError

    async def list_descendants(self, task_id: str) -> list[TaskRow]:
        """Recursive CTE on `parent_task_id`. Used by cancel propagation."""
        raise NotImplementedError

    async def update_task_state(
        self,
        task_id: str,
        new_state: TaskState,
        *,
        status_code: Optional[int] = None,
        output: Optional[dict[str, Any]] = None,
        error: Optional[dict[str, Any]] = None,
    ) -> None:
        """Low-level state write. ONLY callable from `bp_router.state.task_transition`."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Task events (audit)
    # ------------------------------------------------------------------

    async def insert_task_event(
        self,
        *,
        task_id: str,
        kind: str,
        actor_agent_id: Optional[str],
        from_state: Optional[TaskState] = None,
        to_state: Optional[TaskState] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> TaskEventRow:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    async def insert_file(
        self,
        *,
        sha256: str,
        session_id: Optional[str],
        task_id: Optional[str],
        byte_size: int,
        mime_type: Optional[str],
        storage_url: str,
        original_filename: Optional[str],
        expires_at: Optional[datetime],
    ) -> FileRow:
        raise NotImplementedError

    async def get_file(self, file_id: str) -> Optional[FileRow]:
        raise NotImplementedError

    async def get_file_by_sha256(self, sha256: str) -> Optional[FileRow]:
        """Content-addressed dedup lookup; user-scoped."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Cross-user / admin reads (don't fit the Scope wrapper)
# ---------------------------------------------------------------------------


async def get_user_by_id(conn: "asyncpg.Connection", user_id: str) -> Optional[UserRow]:
    raise NotImplementedError


async def get_user_by_email(conn: "asyncpg.Connection", email: str) -> Optional[UserRow]:
    raise NotImplementedError


async def get_agent(conn: "asyncpg.Connection", agent_id: str) -> Optional[AgentRow]:
    raise NotImplementedError


async def list_agents(conn: "asyncpg.Connection") -> list[AgentRow]:
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Sweep helpers (background loops in `bp_router.tasks`)
# ---------------------------------------------------------------------------


async def find_expired_tasks(
    conn: "asyncpg.Connection", *, now: datetime, limit: int = 100
) -> list[TaskRow]:
    """Tasks with deadline < now and a non-terminal state."""
    raise NotImplementedError


async def find_expired_files(
    conn: "asyncpg.Connection", *, now: datetime, limit: int = 1000
) -> list[FileRow]:
    raise NotImplementedError
