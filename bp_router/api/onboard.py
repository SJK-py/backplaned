"""bp_router.api.onboard — Agent registration and token rotation."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from bp_protocol.types import AgentInfo

router = APIRouter()


class OnboardRequest(BaseModel):
    invitation_token: str
    agent_info: AgentInfo
    public_key: Optional[str] = None
    """Optional ed25519 public key for asymmetric auth mode."""


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


@router.post("/onboard", response_model=OnboardResponse)
async def onboard(req: OnboardRequest, request: Request) -> OnboardResponse:
    """Register a new external agent using a one-time invitation token.

    Steps:
      1. Look up invitation by token hash; reject if used or expired.
      2. Persist agent row with status='active', merging the invitation's
         tier/role into the agent's tags.
      3. Mark invitation `used_at`.
      4. Issue agent JWT.
      5. Compute initial `available_destinations` via the ACL evaluator.
      6. Audit: `agent.onboarded`.
    """
    raise HTTPException(status_code=501, detail="not implemented")


@router.post("/agent/refresh-token", response_model=RefreshAgentTokenResponse)
async def refresh_agent_token(
    req: RefreshAgentTokenRequest,
    request: Request,
    authorization: str = Header(..., alias="Authorization"),
) -> RefreshAgentTokenResponse:
    """Rotate an agent's auth token. Requires the current valid token."""
    raise HTTPException(status_code=501, detail="not implemented")
