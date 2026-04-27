"""bp_router.api.admin — Admin-only endpoints (invitations, users, ACL, agents, audit)."""

from __future__ import annotations

import hashlib
import secrets as _secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field

from bp_router.acl import AclEvaluator
from bp_router.acl.rules import AclConfig, load_acl_config_from_dict
from bp_router.db import queries
from bp_router.security.jwt import SessionPrincipal, require_admin
from bp_router.security.passwords import hash_password

router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


class IssueInvitationRequest(BaseModel):
    role: str
    user_tier: Optional[str] = None
    expires_in_s: int = 86_400


class InvitationCreated(BaseModel):
    invitation_token: str
    expires_at: datetime


@router.post("/invitations", response_model=InvitationCreated, status_code=201)
async def issue_invitation(
    req: IssueInvitationRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> InvitationCreated:
    state = request.app.state.bp
    token = _secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(seconds=req.expires_in_s)

    async with state.db_pool.acquire() as conn:
        await queries.insert_invitation(
            conn,
            token_hash=_hash(token),
            role=req.role,
            user_tier=req.user_tier,
            expires_at=expires_at,
            created_by=principal.user_id,
        )
        await queries.append_audit_event(
            conn,
            actor_kind="admin",
            actor_id=principal.user_id,
            event="admin.invitation_issued",
            payload={"role": req.role, "user_tier": req.user_tier},
        )

    return InvitationCreated(invitation_token=token, expires_at=expires_at)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class CreateUserRequest(BaseModel):
    email: EmailStr
    role: str
    user_tier: str = "free"
    initial_password: Optional[str] = None


class UpdateUserRequest(BaseModel):
    role: Optional[str] = None
    user_tier: Optional[str] = None
    suspended: Optional[bool] = None


@router.post("/users", status_code=201)
async def create_user(
    req: CreateUserRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> dict[str, str]:
    state = request.app.state.bp
    auth_kind = "password" if req.initial_password else "api_key"
    auth_secret_hash = hash_password(req.initial_password) if req.initial_password else None

    async with state.db_pool.acquire() as conn:
        existing = await queries.get_user_by_email(conn, req.email)
        if existing is not None:
            raise HTTPException(status_code=409, detail="email already registered")

        user = await queries.insert_user(
            conn,
            email=req.email,
            role=req.role,
            user_tier=req.user_tier,
            auth_kind=auth_kind,
            auth_secret_hash=auth_secret_hash,
        )
        await queries.append_audit_event(
            conn,
            actor_kind="admin",
            actor_id=principal.user_id,
            event="user.created",
            target_kind="user",
            target_id=user.user_id,
            payload={"role": req.role, "user_tier": req.user_tier},
        )
    return {"user_id": user.user_id}


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    req: UpdateUserRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> dict[str, str]:
    state = request.app.state.bp
    fields: list[str] = []
    values: list[Any] = []

    if req.role is not None:
        fields.append("role")
        values.append(req.role)
    if req.user_tier is not None:
        fields.append("user_tier")
        values.append(req.user_tier)
    if req.suspended is True:
        fields.append("suspended_at")
        values.append(_now())
    elif req.suspended is False:
        fields.append("suspended_at")
        values.append(None)

    if not fields:
        return {"user_id": user_id, "updated": "0"}

    set_clause = ", ".join(f"{name} = ${i+2}" for i, name in enumerate(fields))
    sql = f"UPDATE users SET {set_clause} WHERE user_id = $1 RETURNING user_id"

    async with state.db_pool.acquire() as conn:
        row = await conn.fetchrow(sql, user_id, *values)
        if row is None:
            raise HTTPException(status_code=404, detail="user not found")
        await queries.append_audit_event(
            conn,
            actor_kind="admin",
            actor_id=principal.user_id,
            event="user.updated",
            target_kind="user",
            target_id=user_id,
            payload=req.model_dump(exclude_none=True),
        )
    return {"user_id": user_id, "updated": str(len(fields))}


# ---------------------------------------------------------------------------
# ACL
# ---------------------------------------------------------------------------


@router.get("/acl/rules", response_model=AclConfig)
async def get_acl_rules(
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> AclConfig:
    # The active config is held by the evaluator; expose it.
    return request.app.state.bp.acl._config  # type: ignore[attr-defined]


@router.put("/acl/rules")
async def replace_acl_rules(
    config: AclConfig,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> dict[str, Any]:
    state = request.app.state.bp
    # Validate (already done by Pydantic) — persist and hot-reload.
    async with state.db_pool.acquire() as conn:
        count = await queries.replace_acl_rules(
            conn,
            [r.model_dump() for r in config.rules],
            created_by=principal.user_id,
        )
        await queries.append_audit_event(
            conn,
            actor_kind="admin",
            actor_id=principal.user_id,
            event="acl.rules_replaced",
            payload={"rule_count": count},
        )
    state.acl.replace(config)
    return {"rule_count": count}


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


@router.get("/agents")
async def list_agents(
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> list[dict[str, Any]]:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        rows = await queries.list_agents(conn)
    return [
        {
            "agent_id": r.agent_id,
            "kind": r.kind,
            "status": r.status,
            "tags": r.tags,
            "capabilities": r.capabilities,
            "registered_at": r.registered_at.isoformat() if r.registered_at else None,
            "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
        }
        for r in rows
    ]


@router.post("/agents/{agent_id}/suspend", status_code=202)
async def suspend_agent(
    agent_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> dict[str, Any]:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        agent = await queries.get_agent(conn, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        await queries.suspend_agent(conn, agent_id)
        await queries.append_audit_event(
            conn,
            actor_kind="admin",
            actor_id=principal.user_id,
            event="agent.suspended",
            target_kind="agent",
            target_id=agent_id,
        )

    # Force-close the live socket if present.
    entry = state.socket_registry.get(agent_id)
    if entry is not None:
        try:
            await entry.websocket.close(code=4003, reason="agent_suspended")
        except Exception:  # noqa: BLE001
            pass
        entry.closed.set()

    # Fail any in-flight tasks.
    from bp_router.tasks import fail_inflight_for_agent  # noqa: PLC0415

    failed = await fail_inflight_for_agent(state, agent_id, reason="agent_suspended")
    return {"agent_id": agent_id, "failed_tasks": failed}


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class AuditQuery(BaseModel):
    since: Optional[datetime] = None
    until: Optional[datetime] = None
    event: Optional[str] = None
    actor_id: Optional[str] = None
    limit: int = Field(default=100, ge=1, le=1000)


@router.get("/audit")
async def get_audit_log(
    request: Request,
    since: Optional[datetime] = Query(default=None),
    until: Optional[datetime] = Query(default=None),
    event: Optional[str] = Query(default=None),
    actor_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    principal: SessionPrincipal = Depends(require_admin),
) -> list[dict[str, Any]]:
    state = request.app.state.bp
    clauses: list[str] = []
    values: list[Any] = []

    def _add(clause: str, value: Any) -> None:
        values.append(value)
        clauses.append(clause.replace("?", f"${len(values)}"))

    if since is not None:
        _add("ts >= ?", since)
    if until is not None:
        _add("ts <= ?", until)
    if event is not None:
        _add("event = ?", event)
    if actor_id is not None:
        _add("actor_id = ?", actor_id)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT event_id, ts, actor_kind, actor_id, event,
               target_kind, target_id, payload, prev_hash, self_hash
        FROM audit_log
        {where}
        ORDER BY ts DESC, event_id DESC
        LIMIT ${len(values) + 1}
    """
    values.append(limit)

    async with state.db_pool.acquire() as conn:
        rows = await conn.fetch(sql, *values)
    return [dict(r) for r in rows]
