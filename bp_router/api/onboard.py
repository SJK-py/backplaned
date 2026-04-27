"""bp_router.api.onboard — Agent registration and token rotation."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from bp_protocol import PROTOCOL_VERSION
from bp_protocol.types import AgentInfo
from bp_router.acl.evaluator import Caller
from bp_router.db import queries
from bp_router.security.jwt import (
    TokenError,
    issue_agent_token,
    verify_agent_token,
)
from bp_router.visibility import available_destinations

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class OnboardRequest(BaseModel):
    invitation_token: str
    agent_info: AgentInfo
    public_key: Optional[str] = None


class OnboardResponse(BaseModel):
    agent_id: str
    auth_token: str
    expires_at: datetime
    available_destinations: dict[str, Any]


class RefreshAgentTokenRequest(BaseModel):
    agent_id: str


class RefreshAgentTokenResponse(BaseModel):
    auth_token: str
    expires_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/onboard", response_model=OnboardResponse)
async def onboard(req: OnboardRequest, request: Request) -> OnboardResponse:
    state = request.app.state.bp
    settings = state.settings
    pool = state.db_pool

    invitation_hash = _hash_token(req.invitation_token)

    async with pool.acquire() as conn:
        async with conn.transaction():
            invitation = await queries.consume_invitation(
                conn,
                token_hash=invitation_hash,
                used_by=req.agent_info.agent_id,
            )
            if invitation is None:
                raise HTTPException(
                    status_code=403, detail="invalid or used invitation token"
                )

            existing = await queries.get_agent(conn, req.agent_info.agent_id)
            if existing is not None and existing.status != "pending":
                raise HTTPException(
                    status_code=409,
                    detail=f"agent {req.agent_info.agent_id!r} already registered",
                )

            tags = list(req.agent_info.tags)
            invitation_role = invitation.get("role")
            if invitation_role and invitation_role not in tags:
                tags.append(invitation_role)
            user_tier = invitation.get("user_tier")
            if user_tier:
                tags.append(f"user_tier:{user_tier}")

            if existing is None:
                agent_row = await queries.insert_agent(
                    conn,
                    agent_id=req.agent_info.agent_id,
                    kind="external",
                    capabilities=req.agent_info.capabilities,
                    requires_capabilities=req.agent_info.requires_capabilities,
                    tags=tags,
                    agent_info=req.agent_info.model_dump(),
                    public_key=req.public_key,
                )
            else:
                agent_row = existing

            await queries.append_audit_event(
                conn,
                actor_kind="agent",
                actor_id=req.agent_info.agent_id,
                event="agent.onboarded",
                target_kind="agent",
                target_id=req.agent_info.agent_id,
                payload={"role": invitation_role, "tier": user_tier},
            )

    # Issue the agent JWT.
    token, expires_at, jti = issue_agent_token(
        agent_id=agent_row.agent_id,
        secret=settings.jwt_secret.get_secret_value(),
        ttl_s=settings.agent_token_ttl_s,
        key_version=settings.jwt_key_version,
        protocol_version=PROTOCOL_VERSION,
        algorithm=settings.jwt_algorithm,
    )

    # Compute initial visible catalog.
    async with pool.acquire() as conn:
        all_agents = await queries.list_agents(conn)
    caller = Caller(
        agent_id=agent_row.agent_id,
        tags=frozenset(agent_row.tags),
        capabilities=frozenset(agent_row.capabilities),
        requires_capabilities=frozenset(agent_row.requires_capabilities),
        role=None,
        user_tier=None,
    )
    catalog = available_destinations(caller, all_agents, state.acl)

    logger.info(
        "agent_onboarded",
        extra={"event": "agent_onboarded", "bp.agent_id": agent_row.agent_id},
    )
    return OnboardResponse(
        agent_id=agent_row.agent_id,
        auth_token=token,
        expires_at=expires_at,
        available_destinations=catalog,
    )


@router.post("/agent/refresh-token", response_model=RefreshAgentTokenResponse)
async def refresh_agent_token(
    req: RefreshAgentTokenRequest,
    request: Request,
    authorization: str = Header(..., alias="Authorization"),
) -> RefreshAgentTokenResponse:
    """Rotate an agent's auth token. Requires the current valid token."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[len("bearer "):].strip()

    state = request.app.state.bp
    settings = state.settings
    revoked = await _redis_revoked_jti(state)

    try:
        principal = verify_agent_token(
            token,
            secret=settings.jwt_secret.get_secret_value(),
            revoked_jti=revoked,
            key_version=settings.jwt_key_version,
            algorithm=settings.jwt_algorithm,
        )
    except TokenError:
        raise HTTPException(status_code=401, detail="invalid token")

    if principal.agent_id != req.agent_id:
        raise HTTPException(status_code=403, detail="agent_id mismatch")

    # Issue a new token. (Optionally: revoke the old jti — kept simple here.)
    new_token, expires_at, _jti = issue_agent_token(
        agent_id=principal.agent_id,
        secret=settings.jwt_secret.get_secret_value(),
        ttl_s=settings.agent_token_ttl_s,
        key_version=settings.jwt_key_version,
        protocol_version=PROTOCOL_VERSION,
        algorithm=settings.jwt_algorithm,
    )
    return RefreshAgentTokenResponse(auth_token=new_token, expires_at=expires_at)


async def _redis_revoked_jti(state) -> set[str]:  # type: ignore[no-untyped-def]
    if state.redis is None:
        return set()
    members = await state.redis.smembers("router:revoked_jti")
    return set(members) if members else set()
