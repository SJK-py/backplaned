"""bp_router.api.auth — Login and refresh-token rotation."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    totp: Optional[str] = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    expires_at: datetime
    role: str
    user_tier: str


class RefreshRequest(BaseModel):
    refresh_token: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/login", response_model=TokenPair)
async def login(req: LoginRequest, request: Request) -> TokenPair:
    """Issue a session JWT + refresh token after verifying credentials.

    Steps:
      1. Look up user by email.
      2. Verify argon2id hash via `bp_router.security.passwords.verify`.
      3. If user has TOTP enabled, verify `req.totp`.
      4. Issue session JWT (`bp_router.security.jwt.issue_session_token`).
      5. Issue refresh token, persist hash in `auth_refresh_tokens`.
      6. Append audit_log entry (auth.login_succeeded / auth.login_failed).
    """
    raise HTTPException(status_code=501, detail="not implemented")


@router.post("/refresh", response_model=TokenPair)
async def refresh(req: RefreshRequest, request: Request) -> TokenPair:
    """Exchange a refresh token for a new pair.

    Single-use semantics — replaying a used token invalidates the entire
    family and emits `auth.refresh_replayed`. See
    `docs/design/security.md` §5.3.
    """
    raise HTTPException(status_code=501, detail="not implemented")
