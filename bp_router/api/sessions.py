"""bp_router.api.sessions — Open / list / close user sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bp_router.db import queries
from bp_router.security.jwt import SessionPrincipal, require_user
from bp_router.tasks import cancel_task

router = APIRouter()


class OpenSessionRequest(BaseModel):
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionView(BaseModel):
    session_id: str
    opened_at: datetime
    closed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskSummaryView(BaseModel):
    task_id: str
    parent_task_id: str | None
    state: str
    status_code: int | None = None
    agent_id: str
    created_at: datetime
    updated_at: datetime


def _session_to_view(row) -> SessionView:  # type: ignore[no-untyped-def]
    return SessionView(
        session_id=row.session_id,
        opened_at=row.opened_at,
        closed_at=row.closed_at,
        metadata=row.metadata,
    )


@router.post("", response_model=SessionView, status_code=201)
async def open_session(
    req: OpenSessionRequest,
    request: Request,
    principal: SessionPrincipal = Depends(require_user),
) -> SessionView:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        row = await queries.Scope.user(conn, principal.user_id).open_session(
            metadata=req.metadata
        )
        await queries.append_audit_event(
            conn,
            actor_kind="user",
            actor_id=principal.user_id,
            event="session.opened",
            target_kind="session",
            target_id=row.session_id,
        )
    return _session_to_view(row)


@router.delete("/{session_id}", status_code=204)
async def close_session(
    session_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_user),
) -> None:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        scope = queries.Scope.user(conn, principal.user_id)
        existing = await scope.get_session(session_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="session not found")
        if existing.closed_at is not None:
            return None  # idempotent

        # Cancel any in-flight tasks within this session.
        rows = await conn.fetch(
            """
            SELECT task_id FROM tasks
            WHERE user_id = $1 AND session_id = $2
              AND state IN ('QUEUED','RUNNING','WAITING_CHILDREN')
            """,
            principal.user_id,
            session_id,
        )

    for r in rows:
        await cancel_task(
            state,
            r["task_id"],
            user_id=principal.user_id,
            reason="session_closed",
            initiator=principal.user_id,
        )

    async with state.db_pool.acquire() as conn:
        await queries.Scope.user(conn, principal.user_id).close_session(session_id)
        await queries.append_audit_event(
            conn,
            actor_kind="user",
            actor_id=principal.user_id,
            event="session.closed",
            target_kind="session",
            target_id=session_id,
        )


@router.get("", response_model=list[SessionView])
async def list_sessions(
    request: Request,
    principal: SessionPrincipal = Depends(require_user),
) -> list[SessionView]:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        rows = await queries.Scope.user(conn, principal.user_id).list_sessions()
    return [_session_to_view(r) for r in rows]


@router.get("/{session_id}/tasks", response_model=list[TaskSummaryView])
async def list_session_tasks(
    session_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_user),
) -> list[TaskSummaryView]:
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        scope = queries.Scope.user(conn, principal.user_id)
        if await scope.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail="session not found")
        rows = await scope.list_session_tasks(session_id)
    return [
        TaskSummaryView(
            task_id=r.task_id,
            parent_task_id=r.parent_task_id,
            state=r.state.value,
            status_code=r.status_code,
            agent_id=r.agent_id,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]
