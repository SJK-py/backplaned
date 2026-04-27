"""bp_router.api.admin — Admin-only endpoints (invitations, users, ACL, agents, audit)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from bp_router.acl.rules import AclConfig
from bp_router.security.jwt import SessionPrincipal, require_admin

router = APIRouter()


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


class IssueInvitationRequest(BaseModel):
    role: str  # for agent: maps to a tier; for user: 'user' or 'admin'
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
    raise HTTPException(status_code=501, detail="not implemented")


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
    raise HTTPException(status_code=501, detail="not implemented")


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    req: UpdateUserRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> dict[str, str]:
    raise HTTPException(status_code=501, detail="not implemented")


# ---------------------------------------------------------------------------
# ACL
# ---------------------------------------------------------------------------


@router.get("/acl/rules")
async def get_acl_rules(
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> AclConfig:
    raise HTTPException(status_code=501, detail="not implemented")


@router.put("/acl/rules")
async def replace_acl_rules(
    config: AclConfig,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> dict[str, Any]:
    """Validate, run acl.tests.yaml, persist, hot-reload the evaluator.

    See `docs/design/acl.md` §10 for testing semantics.
    """
    raise HTTPException(status_code=501, detail="not implemented")


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


@router.get("/agents")
async def list_agents(
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> list[dict[str, Any]]:
    raise HTTPException(status_code=501, detail="not implemented")


@router.post("/agents/{agent_id}/suspend", status_code=202)
async def suspend_agent(
    agent_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_admin),
) -> dict[str, Any]:
    """Mark agent suspended, force-close any live socket, fail in-flight tasks."""
    raise HTTPException(status_code=501, detail="not implemented")


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
    query: AuditQuery = Depends(),  # noqa: B008
    request: Request = None,  # type: ignore[assignment]
    principal: SessionPrincipal = Depends(require_admin),
) -> list[dict[str, Any]]:
    raise HTTPException(status_code=501, detail="not implemented")
