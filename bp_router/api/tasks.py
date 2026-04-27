"""bp_router.api.tasks — Read task status, cancel tasks."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from bp_protocol.types import TaskState
from bp_router.db import queries
from bp_router.security.jwt import SessionPrincipal, require_user
from bp_router.tasks import cancel_task

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
    actor_agent_id: str | None = None
    from_state: TaskState | None = None
    to_state: TaskState | None = None
    payload: dict[str, Any] = {}


class TaskDetailView(TaskView):
    events: list[TaskEventView] = []


def _task_to_view(row) -> TaskView:  # type: ignore[no-untyped-def]
    return TaskView(
        task_id=row.task_id,
        parent_task_id=row.parent_task_id,
        state=row.state,
        status_code=row.status_code,
        agent_id=row.agent_id,
        session_id=row.session_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deadline=row.deadline,
        output=row.output,
        error=row.error,
    )


def _event_to_view(row) -> TaskEventView:  # type: ignore[no-untyped-def]
    return TaskEventView(
        ts=row.ts,
        kind=row.kind,
        actor_agent_id=row.actor_agent_id,
        from_state=row.from_state,
        to_state=row.to_state,
        payload=row.payload,
    )


@router.get("/{task_id}", response_model=TaskDetailView)
async def get_task(
    task_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_user),
) -> TaskDetailView:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        scope = queries.Scope.user(conn, principal.user_id)
        row = await scope.get_task(task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="task not found")
        events = await scope.list_task_events(task_id)

    base = _task_to_view(row)
    return TaskDetailView(**base.model_dump(), events=[_event_to_view(e) for e in events])


@router.post("/{task_id}/cancel", status_code=202)
async def cancel(
    task_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_user),
) -> dict[str, Any]:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        row = await queries.Scope.user(conn, principal.user_id).get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")

    cancelled = await cancel_task(
        state,
        task_id,
        user_id=principal.user_id,
        reason="user_aborted",
        initiator=principal.user_id,
    )
    return {"task_id": task_id, "cancelled_count": cancelled}
