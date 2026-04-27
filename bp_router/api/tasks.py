"""bp_router.api.tasks — Read task status, cancel tasks."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from bp_protocol.types import TaskState
from bp_router.security.jwt import SessionPrincipal, require_user

router = APIRouter()


class TaskView(BaseModel):
    task_id: str
    parent_task_id: str | None = None
    state: TaskState
    status_code: int | None = None
    agent_id: str
    session_id: str
    created_at: datetime
    updated_at: datetime
    deadline: datetime | None = None
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class TaskEventView(BaseModel):
    ts: datetime
    kind: str
    from_state: TaskState | None = None
    to_state: TaskState | None = None
    payload: dict[str, Any] = {}


class TaskDetailView(TaskView):
    events: list[TaskEventView] = []


@router.get("/{task_id}", response_model=TaskDetailView)
async def get_task(
    task_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_user),
) -> TaskDetailView:
    """Read one task plus its event timeline. user-scoped."""
    raise HTTPException(status_code=501, detail="not implemented")


@router.post("/{task_id}/cancel", status_code=202)
async def cancel(
    task_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_user),
) -> dict[str, Any]:
    """Cancel a task and all its descendants. user-scoped."""
    raise HTTPException(status_code=501, detail="not implemented")
