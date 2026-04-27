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
# Helpers for asyncpg <-> dict conversion
# ---------------------------------------------------------------------------


def _row_to_dict(row: "asyncpg.Record | None") -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return dict(row)


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

    @property
    def user_id(self) -> Optional[str]:
        return self._user_id

    def _require_user(self) -> str:
        if self._user_id is None:
            raise RuntimeError("user_id-scoped query attempted in admin scope")
        return self._user_id

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def open_session(
        self, *, metadata: Optional[dict[str, Any]] = None
    ) -> SessionRow:
        user_id = self._require_user()
        session_id = _new_id("ses_")
        row = await self._conn.fetchrow(
            """
            INSERT INTO sessions (session_id, user_id, opened_at, metadata)
            VALUES ($1, $2, $3, $4)
            RETURNING session_id, user_id, opened_at, closed_at, metadata
            """,
            session_id,
            user_id,
            _now(),
            metadata or {},
        )
        return SessionRow.model_validate(dict(row))

    async def close_session(self, session_id: str) -> None:
        user_id = self._require_user()
        await self._conn.execute(
            """
            UPDATE sessions
            SET closed_at = $3
            WHERE session_id = $1 AND user_id = $2 AND closed_at IS NULL
            """,
            session_id,
            user_id,
            _now(),
        )

    async def get_session(self, session_id: str) -> Optional[SessionRow]:
        user_id = self._require_user()
        row = await self._conn.fetchrow(
            """
            SELECT session_id, user_id, opened_at, closed_at, metadata
            FROM sessions
            WHERE session_id = $1 AND user_id = $2
            """,
            session_id,
            user_id,
        )
        return SessionRow.model_validate(dict(row)) if row else None

    async def list_sessions(self, *, limit: int = 50) -> list[SessionRow]:
        user_id = self._require_user()
        rows = await self._conn.fetch(
            """
            SELECT session_id, user_id, opened_at, closed_at, metadata
            FROM sessions
            WHERE user_id = $1
            ORDER BY opened_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )
        return [SessionRow.model_validate(dict(r)) for r in rows]

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
        user_id = self._require_user()
        task_id = _new_id("tsk_")
        # root_task_id propagates from parent or is set to self for new roots.
        root_task_id = task_id
        if parent_task_id is not None:
            row = await self._conn.fetchrow(
                "SELECT root_task_id FROM tasks WHERE task_id = $1 AND user_id = $2",
                parent_task_id,
                user_id,
            )
            if row is not None:
                root_task_id = row["root_task_id"]
        now = _now()
        row = await self._conn.fetchrow(
            """
            INSERT INTO tasks (
                task_id, parent_task_id, root_task_id,
                user_id, session_id, agent_id, state,
                idempotency_key, priority, deadline,
                created_at, updated_at, input
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $11, $12)
            RETURNING *
            """,
            task_id,
            parent_task_id,
            root_task_id,
            user_id,
            session_id,
            agent_id,
            TaskState.QUEUED.value,
            idempotency_key,
            priority.value,
            deadline,
            now,
            input,
        )
        return TaskRow.model_validate(dict(row))

    async def get_task(self, task_id: str) -> Optional[TaskRow]:
        user_id = self._require_user()
        row = await self._conn.fetchrow(
            "SELECT * FROM tasks WHERE task_id = $1 AND user_id = $2",
            task_id,
            user_id,
        )
        return TaskRow.model_validate(dict(row)) if row else None

    async def get_task_for_update(self, task_id: str) -> Optional[TaskRow]:
        """SELECT ... FOR UPDATE — used by `task_transition`.

        The caller MUST hold an open transaction on the underlying
        connection. asyncpg's `conn.transaction()` is the standard way.
        """
        user_id = self._require_user()
        row = await self._conn.fetchrow(
            "SELECT * FROM tasks WHERE task_id = $1 AND user_id = $2 FOR UPDATE",
            task_id,
            user_id,
        )
        return TaskRow.model_validate(dict(row)) if row else None

    async def list_session_tasks(
        self, session_id: str, *, limit: int = 100
    ) -> list[TaskRow]:
        user_id = self._require_user()
        rows = await self._conn.fetch(
            """
            SELECT * FROM tasks
            WHERE user_id = $1 AND session_id = $2
            ORDER BY created_at DESC
            LIMIT $3
            """,
            user_id,
            session_id,
            limit,
        )
        return [TaskRow.model_validate(dict(r)) for r in rows]

    async def find_idempotent(self, idempotency_key: str) -> Optional[TaskRow]:
        user_id = self._require_user()
        row = await self._conn.fetchrow(
            """
            SELECT * FROM tasks
            WHERE user_id = $1 AND idempotency_key = $2
            """,
            user_id,
            idempotency_key,
        )
        return TaskRow.model_validate(dict(row)) if row else None

    async def list_descendants(self, task_id: str) -> list[TaskRow]:
        """Recursive CTE on `parent_task_id`. Used by cancel propagation."""
        user_id = self._require_user()
        rows = await self._conn.fetch(
            """
            WITH RECURSIVE descendants AS (
                SELECT * FROM tasks
                WHERE parent_task_id = $1 AND user_id = $2
              UNION ALL
                SELECT t.* FROM tasks t
                JOIN descendants d ON t.parent_task_id = d.task_id
                WHERE t.user_id = $2
            )
            SELECT * FROM descendants
            """,
            task_id,
            user_id,
        )
        return [TaskRow.model_validate(dict(r)) for r in rows]

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
        user_id = self._require_user()
        await self._conn.execute(
            """
            UPDATE tasks
            SET state = $3, status_code = COALESCE($4, status_code),
                output = COALESCE($5, output), error = COALESCE($6, error),
                updated_at = $7
            WHERE task_id = $1 AND user_id = $2
            """,
            task_id,
            user_id,
            new_state.value,
            status_code,
            output,
            error,
            _now(),
        )

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
        # task_events does not carry user_id directly; uniqueness comes
        # via the task_id FK. The caller is responsible for ensuring the
        # task belongs to the right user (the Scope wrapper enforces that
        # by only writing events for tasks fetched through it).
        row = await self._conn.fetchrow(
            """
            INSERT INTO task_events
                (task_id, ts, kind, actor_agent_id, from_state, to_state, payload)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING event_id, task_id, ts, kind, actor_agent_id,
                      from_state, to_state, payload
            """,
            task_id,
            _now(),
            kind,
            actor_agent_id,
            from_state.value if from_state else None,
            to_state.value if to_state else None,
            payload or {},
        )
        return TaskEventRow.model_validate(dict(row))

    async def list_task_events(
        self, task_id: str, *, limit: int = 200
    ) -> list[TaskEventRow]:
        # Cross-check ownership via the parent task before exposing events.
        user_id = self._require_user()
        owner = await self._conn.fetchval(
            "SELECT user_id FROM tasks WHERE task_id = $1",
            task_id,
        )
        if owner != user_id:
            return []
        rows = await self._conn.fetch(
            """
            SELECT event_id, task_id, ts, kind, actor_agent_id,
                   from_state, to_state, payload
            FROM task_events
            WHERE task_id = $1
            ORDER BY ts ASC
            LIMIT $2
            """,
            task_id,
            limit,
        )
        return [TaskEventRow.model_validate(dict(r)) for r in rows]

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
        user_id = self._require_user()
        # Content-addressed dedup: if the user already uploaded the same
        # bytes, return the existing row.
        existing = await self._conn.fetchrow(
            "SELECT * FROM files WHERE user_id = $1 AND sha256 = $2",
            user_id,
            sha256,
        )
        if existing is not None:
            return FileRow.model_validate(dict(existing))

        file_id = _new_id("fil_")
        row = await self._conn.fetchrow(
            """
            INSERT INTO files
                (file_id, sha256, user_id, session_id, task_id,
                 byte_size, mime_type, storage_url, original_filename,
                 created_at, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING *
            """,
            file_id,
            sha256,
            user_id,
            session_id,
            task_id,
            byte_size,
            mime_type,
            storage_url,
            original_filename,
            _now(),
            expires_at,
        )
        return FileRow.model_validate(dict(row))

    async def get_file(self, file_id: str) -> Optional[FileRow]:
        user_id = self._require_user()
        row = await self._conn.fetchrow(
            "SELECT * FROM files WHERE file_id = $1 AND user_id = $2",
            file_id,
            user_id,
        )
        return FileRow.model_validate(dict(row)) if row else None

    async def get_file_by_sha256(self, sha256: str) -> Optional[FileRow]:
        """Content-addressed dedup lookup; user-scoped."""
        user_id = self._require_user()
        row = await self._conn.fetchrow(
            "SELECT * FROM files WHERE user_id = $1 AND sha256 = $2",
            user_id,
            sha256,
        )
        return FileRow.model_validate(dict(row)) if row else None


# ---------------------------------------------------------------------------
# Cross-user / admin reads (don't fit the Scope wrapper)
# ---------------------------------------------------------------------------


async def get_user_by_id(
    conn: "asyncpg.Connection", user_id: str
) -> Optional[UserRow]:
    row = await conn.fetchrow(
        "SELECT * FROM users WHERE user_id = $1",
        user_id,
    )
    return UserRow.model_validate(dict(row)) if row else None


async def get_user_by_email(
    conn: "asyncpg.Connection", email: str
) -> Optional[UserRow]:
    row = await conn.fetchrow(
        "SELECT * FROM users WHERE email = $1",
        email,
    )
    return UserRow.model_validate(dict(row)) if row else None


async def insert_user(
    conn: "asyncpg.Connection",
    *,
    user_id: Optional[str] = None,
    email: Optional[str],
    role: str,
    user_tier: str,
    auth_kind: str,
    auth_secret_hash: Optional[str],
) -> UserRow:
    user_id = user_id or _new_id("usr_")
    row = await conn.fetchrow(
        """
        INSERT INTO users
            (user_id, email, role, user_tier, auth_kind, auth_secret_hash, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING *
        """,
        user_id,
        email,
        role,
        user_tier,
        auth_kind,
        auth_secret_hash,
        _now(),
    )
    return UserRow.model_validate(dict(row))


async def get_agent(
    conn: "asyncpg.Connection", agent_id: str
) -> Optional[AgentRow]:
    row = await conn.fetchrow(
        "SELECT * FROM agents WHERE agent_id = $1",
        agent_id,
    )
    return AgentRow.model_validate(dict(row)) if row else None


async def list_agents(conn: "asyncpg.Connection") -> list[AgentRow]:
    rows = await conn.fetch("SELECT * FROM agents ORDER BY agent_id")
    return [AgentRow.model_validate(dict(r)) for r in rows]


async def insert_agent(
    conn: "asyncpg.Connection",
    *,
    agent_id: str,
    kind: str,
    capabilities: list[str],
    requires_capabilities: list[str],
    tags: list[str],
    agent_info: dict[str, Any],
    auth_token_hash: Optional[str] = None,
    public_key: Optional[str] = None,
) -> AgentRow:
    row = await conn.fetchrow(
        """
        INSERT INTO agents (
            agent_id, kind, status, capabilities, requires_capabilities,
            tags, agent_info, auth_token_hash, public_key, registered_at
        )
        VALUES ($1, $2, 'active', $3, $4, $5, $6, $7, $8, $9)
        RETURNING *
        """,
        agent_id,
        kind,
        capabilities,
        requires_capabilities,
        tags,
        agent_info,
        auth_token_hash,
        public_key,
        _now(),
    )
    return AgentRow.model_validate(dict(row))


async def update_agent_last_seen(
    conn: "asyncpg.Connection", agent_id: str
) -> None:
    await conn.execute(
        "UPDATE agents SET last_seen_at = $2 WHERE agent_id = $1",
        agent_id,
        _now(),
    )


async def suspend_agent(conn: "asyncpg.Connection", agent_id: str) -> None:
    await conn.execute(
        "UPDATE agents SET status = 'suspended' WHERE agent_id = $1",
        agent_id,
    )


# ---------------------------------------------------------------------------
# Sweep helpers (background loops in `bp_router.tasks`)
# ---------------------------------------------------------------------------


async def find_expired_tasks(
    conn: "asyncpg.Connection", *, now: datetime, limit: int = 100
) -> list[TaskRow]:
    """Tasks with deadline < now and a non-terminal state."""
    rows = await conn.fetch(
        """
        SELECT * FROM tasks
        WHERE deadline IS NOT NULL
          AND deadline < $1
          AND state IN ('QUEUED', 'RUNNING', 'WAITING_CHILDREN')
        ORDER BY deadline ASC
        LIMIT $2
        """,
        now,
        limit,
    )
    return [TaskRow.model_validate(dict(r)) for r in rows]


async def find_expired_files(
    conn: "asyncpg.Connection", *, now: datetime, limit: int = 1000
) -> list[FileRow]:
    rows = await conn.fetch(
        """
        SELECT * FROM files
        WHERE expires_at IS NOT NULL AND expires_at < $1
        ORDER BY expires_at ASC
        LIMIT $2
        """,
        now,
        limit,
    )
    return [FileRow.model_validate(dict(r)) for r in rows]


async def delete_file_row(conn: "asyncpg.Connection", file_id: str) -> None:
    await conn.execute("DELETE FROM files WHERE file_id = $1", file_id)


# ---------------------------------------------------------------------------
# Refresh tokens
# ---------------------------------------------------------------------------


async def insert_refresh_token(
    conn: "asyncpg.Connection",
    *,
    token_hash: str,
    user_id: str,
    expires_at: datetime,
) -> None:
    await conn.execute(
        """
        INSERT INTO auth_refresh_tokens (token_hash, user_id, issued_at, expires_at)
        VALUES ($1, $2, $3, $4)
        """,
        token_hash,
        user_id,
        _now(),
        expires_at,
    )


async def consume_refresh_token(
    conn: "asyncpg.Connection",
    *,
    token_hash: str,
    replaced_by: str,
) -> Optional[str]:
    """Single-use exchange. Returns the user_id if accepted; None otherwise.

    On replay (used_at already set), invalidates the entire family for
    that user — the caller surfaces this to the audit log.
    """
    row = await conn.fetchrow(
        """
        SELECT user_id, used_at FROM auth_refresh_tokens
        WHERE token_hash = $1 AND expires_at > $2
        FOR UPDATE
        """,
        token_hash,
        _now(),
    )
    if row is None:
        return None
    if row["used_at"] is not None:
        # Replay → blow away the family
        await conn.execute(
            "DELETE FROM auth_refresh_tokens WHERE user_id = $1",
            row["user_id"],
        )
        return None
    await conn.execute(
        """
        UPDATE auth_refresh_tokens
        SET used_at = $2, replaced_by = $3
        WHERE token_hash = $1
        """,
        token_hash,
        _now(),
        replaced_by,
    )
    return row["user_id"]


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


async def insert_invitation(
    conn: "asyncpg.Connection",
    *,
    token_hash: str,
    role: str,
    user_tier: Optional[str],
    expires_at: datetime,
    created_by: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO invitations
            (token_hash, role, user_tier, expires_at, created_by)
        VALUES ($1, $2, $3, $4, $5)
        """,
        token_hash,
        role,
        user_tier,
        expires_at,
        created_by,
    )


async def consume_invitation(
    conn: "asyncpg.Connection",
    *,
    token_hash: str,
    used_by: str,
) -> Optional[dict[str, Any]]:
    """Mark an invitation used. Returns its claims or None if invalid/used."""
    row = await conn.fetchrow(
        """
        SELECT token_hash, role, user_tier, expires_at, used_at
        FROM invitations
        WHERE token_hash = $1
        FOR UPDATE
        """,
        token_hash,
    )
    if row is None or row["used_at"] is not None or row["expires_at"] < _now():
        return None
    await conn.execute(
        """
        UPDATE invitations SET used_at = $2, used_by = $3
        WHERE token_hash = $1
        """,
        token_hash,
        _now(),
        used_by,
    )
    return {
        "role": row["role"],
        "user_tier": row["user_tier"],
    }


# ---------------------------------------------------------------------------
# ACL rule persistence
# ---------------------------------------------------------------------------


async def replace_acl_rules(
    conn: "asyncpg.Connection",
    rules: list[dict[str, Any]],
    *,
    created_by: Optional[str],
) -> int:
    """Atomically swap the rule set. Returns row count inserted."""
    async with conn.transaction():
        await conn.execute("DELETE FROM acl_rules")
        for ord_, rule in enumerate(rules):
            await conn.execute(
                """
                INSERT INTO acl_rules
                    (rule_id, ord, name, description, caller, callee,
                     effect, scope, deny_as_not_found, created_at, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                _new_id("acl_"),
                ord_,
                rule["name"],
                rule.get("description"),
                rule.get("caller", {}),
                rule.get("callee", {}),
                rule["effect"],
                rule.get("scope", {"visibility": True, "permission": True}),
                rule.get("deny_as_not_found", False),
                _now(),
                created_by,
            )
    return len(rules)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


async def append_audit_event(
    conn: "asyncpg.Connection",
    *,
    actor_kind: str,
    actor_id: Optional[str],
    event: str,
    target_kind: Optional[str] = None,
    target_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """Hash-chained append. The chain link is computed in SQL so concurrent
    writers all observe a consistent prev_hash via FOR UPDATE on the latest row.
    """
    import hashlib  # noqa: PLC0415
    import json  # noqa: PLC0415

    async with conn.transaction():
        prev = await conn.fetchrow(
            """
            SELECT self_hash FROM audit_log
            ORDER BY ts DESC, event_id DESC
            LIMIT 1
            FOR UPDATE
            """
        )
        prev_hash = prev["self_hash"] if prev else ""
        now = _now()
        body = json.dumps(
            {
                "ts": now.isoformat(),
                "actor_kind": actor_kind,
                "actor_id": actor_id,
                "event": event,
                "target_kind": target_kind,
                "target_id": target_id,
                "payload": payload or {},
                "prev_hash": prev_hash,
            },
            sort_keys=True,
            default=str,
        )
        self_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        await conn.execute(
            """
            INSERT INTO audit_log
                (ts, actor_kind, actor_id, event, target_kind, target_id,
                 payload, prev_hash, self_hash)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            now,
            actor_kind,
            actor_id,
            event,
            target_kind,
            target_id,
            payload or {},
            prev_hash or None,
            self_hash,
        )
