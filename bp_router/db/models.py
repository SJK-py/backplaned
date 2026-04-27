"""bp_router.db.models — Row dataclasses for DB results.

Pydantic-validated for runtime safety; cheap to instantiate from
asyncpg `Record` rows via `Model.model_validate(dict(record))`.

Schema is owned by Alembic migrations. Adding a column means adding a
migration AND updating the model here. CI checks the two stay in sync.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from bp_protocol.types import TaskPriority, TaskState


class _Row(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


# ---------------------------------------------------------------------------
# Identity / users
# ---------------------------------------------------------------------------


class UserRow(_Row):
    user_id: str
    role: str  # admin | user | service
    user_tier: str  # free | paid | enterprise | <custom>
    auth_kind: str  # password | oidc | api_key
    auth_secret_hash: Optional[str]  # password hash, OIDC sub, or API key hash
    email: Optional[str]
    created_at: datetime
    suspended_at: Optional[datetime] = None


class SessionRow(_Row):
    session_id: str
    user_id: str
    opened_at: datetime
    closed_at: Optional[datetime] = None
    metadata: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class AgentRow(_Row):
    agent_id: str
    kind: str  # external | embedded
    status: str  # active | suspended | pending
    capabilities: list[str]
    requires_capabilities: list[str]
    tags: list[str]
    agent_info: dict[str, Any]
    auth_token_hash: Optional[str]
    public_key: Optional[str]
    registered_at: datetime
    last_seen_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class TaskRow(_Row):
    task_id: str
    parent_task_id: Optional[str]
    root_task_id: str
    user_id: str
    session_id: str
    agent_id: str
    state: TaskState
    status_code: Optional[int] = None
    idempotency_key: Optional[str] = None
    priority: TaskPriority = TaskPriority.NORMAL
    deadline: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    input: dict[str, Any] = {}
    output: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None


class TaskEventRow(_Row):
    event_id: str
    task_id: str
    ts: datetime
    kind: str  # transition | dispatch | ack | grant | progress
    actor_agent_id: Optional[str]
    from_state: Optional[TaskState] = None
    to_state: Optional[TaskState] = None
    payload: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


class FileRow(_Row):
    file_id: str
    sha256: str
    user_id: str
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    byte_size: int
    mime_type: Optional[str] = None
    storage_url: str  # backend-internal locator (e.g. s3://bucket/key)
    original_filename: Optional[str] = None
    created_at: datetime
    expires_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# ACL / audit / invitations
# ---------------------------------------------------------------------------


class AclRuleRow(_Row):
    rule_id: str
    ord: int
    name: str
    description: Optional[str] = None
    caller: dict[str, Any]
    callee: dict[str, Any]
    effect: str  # allow | deny
    scope: dict[str, Any] = {"visibility": True, "permission": True}
    deny_as_not_found: bool = False
    created_at: datetime
    created_by: Optional[str] = None


class AuditLogRow(_Row):
    event_id: str
    ts: datetime
    actor_kind: str  # user | agent | admin | system
    actor_id: Optional[str]
    event: str
    target_kind: Optional[str] = None
    target_id: Optional[str] = None
    payload: dict[str, Any] = {}
    prev_hash: Optional[str] = None
    self_hash: str


class InvitationRow(_Row):
    token_hash: str
    role: str  # for agent: tier:N maybe; for user: user/admin
    user_tier: Optional[str] = None
    expires_at: datetime
    used_at: Optional[datetime] = None
    used_by: Optional[str] = None
    created_by: str


class RefreshTokenRow(_Row):
    token_hash: str
    user_id: str
    issued_at: datetime
    expires_at: datetime
    used_at: Optional[datetime] = None
    replaced_by: Optional[str] = None
