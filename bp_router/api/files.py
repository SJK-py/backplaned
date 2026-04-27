"""bp_router.api.files — Upload and download ProxyFiles."""

from __future__ import annotations

from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import RedirectResponse, StreamingResponse

from bp_router.security.jwt import SessionPrincipal, require_user

router = APIRouter()


@router.post("", status_code=201)
async def upload(
    file: UploadFile,
    request: Request,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    principal: SessionPrincipal = Depends(require_user),
) -> dict[str, str]:
    """Stream-upload a file to the configured backend.

    Steps:
      1. Quota check (file_storage_bytes).
      2. Stream to FileStore.put() with sha256 verification.
      3. Insert files row (content-addressed dedup if sha256 already exists for user).
      4. Return ProxyFile-shaped reference.
    """
    raise HTTPException(status_code=501, detail="not implemented")


@router.get("/{file_id}")
async def download(
    file_id: str,
    request: Request,
    principal: SessionPrincipal = Depends(require_user),
) -> Response:
    """Stream a file or 302 to its presigned URL.

    Permission: caller must be the owning user (or an agent acting in
    that user's session — proven via the session JWT's user_id).
    """
    raise HTTPException(status_code=501, detail="not implemented")
