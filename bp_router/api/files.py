"""bp_router.api.files — Upload and download ProxyFiles."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import RedirectResponse, StreamingResponse

from bp_router.db import queries
from bp_router.security.jwt import SessionPrincipal, require_user
from bp_router.storage.base import FileMeta

router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.post("", status_code=201)
async def upload(
    file: UploadFile,
    request: Request,
    session_id: Optional[str] = Query(default=None),
    task_id: Optional[str] = Query(default=None),
    principal: SessionPrincipal = Depends(require_user),
) -> dict[str, str]:
    """Stream-upload a file. Content-addressed by sha256."""
    state = request.app.state.bp
    settings = state.settings
    file_store = state.file_store

    # Hash + size while streaming to a temp buffer. We can't avoid the
    # buffer because content-addressed storage needs the hash before the
    # backend put can begin (sha256 is the key). For small files this is
    # fine; large uploads should use presigned URLs (out of scope here).
    h = hashlib.sha256()
    size = 0
    chunks: list[bytes] = []
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        h.update(chunk)
        size += len(chunk)
        chunks.append(chunk)

    if size == 0:
        raise HTTPException(status_code=400, detail="empty upload")

    sha256 = h.hexdigest()
    mime_type = file.content_type or None

    async def _src() -> AsyncIterator[bytes]:
        for c in chunks:
            yield c

    storage_url = await file_store.put(
        sha256,
        _src(),
        FileMeta(
            sha256=sha256,
            byte_size=size,
            mime_type=mime_type,
            original_filename=file.filename,
        ),
    )

    expires_at = _now() + timedelta(seconds=settings.file_default_ttl_s)
    async with state.db_pool.acquire() as conn:
        row = await queries.Scope.user(conn, principal.user_id).insert_file(
            sha256=sha256,
            session_id=session_id,
            task_id=task_id,
            byte_size=size,
            mime_type=mime_type,
            storage_url=storage_url,
            original_filename=file.filename,
            expires_at=expires_at,
        )

    return {
        "file_id": row.file_id,
        "sha256": row.sha256,
        "byte_size": str(row.byte_size),
        "path": f"/v1/files/{row.file_id}",
        "protocol": "router-proxy",
    }


@router.get("/{file_id}")
async def download(
    file_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_user),
):  # type: ignore[no-untyped-def]
    """Stream a file or 302 to its presigned URL."""
    state = request.app.state.bp
    async with state.db_pool.acquire() as conn:
        row = await queries.Scope.user(conn, principal.user_id).get_file(file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="file not found")

    file_store = state.file_store

    # Prefer presigned URL when the backend supports it.
    presigned = await file_store.presigned_url(row.sha256, ttl_s=300)
    if presigned:
        return RedirectResponse(presigned, status_code=302)

    headers = {}
    if row.original_filename:
        headers["Content-Disposition"] = f'attachment; filename="{row.original_filename}"'
    headers["Content-Length"] = str(row.byte_size)
    if row.mime_type:
        media_type = row.mime_type
    else:
        media_type = "application/octet-stream"

    stream = await file_store.open(row.sha256)
    return StreamingResponse(stream, media_type=media_type, headers=headers)
