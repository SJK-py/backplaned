"""bp_router.api.sessions — Open / list / close user sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bp_router.security.jwt import SessionPrincipal, require_user

router = APIRouter()


class OpenSessionRequest(BaseModel):
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionView(BaseModel):
    session_id: str
    opened_at: datetime
    closed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post("", response_model=SessionView, status_code=201)
async def open_session(
    req: OpenSessionRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_user),
) -> SessionView:
    """Open a fresh session. Returns the new session_id."""
    raise HTTPException(status_code=501, detail="not implemented")


@router.delete("/{session_id}", status_code=204)
async def close_session(
    session_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_user),
) -> None:
    """Close a session. In-flight tasks are cancelled with reason
    `session_closed`.
    """
    raise HTTPException(status_code=501, detail="not implemented")


@router.get("", response_model=list[SessionView])
async def list_sessions(
    request: Request,
    principal: SessionPrincipal = Depends(require_user),
) -> list[SessionView]:
    raise HTTPException(status_code=501, detail="not implemented")


@router.get("/{session_id}/tasks")
async def list_session_tasks(
    session_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_user),
) -> list[dict[str, Any]]:
    raise HTTPException(status_code=501, detail="not implemented")
