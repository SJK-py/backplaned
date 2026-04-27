"""bp_router.api.auth — Login and refresh-token rotation."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr

from bp_router.db import queries
from bp_router.security.jwt import issue_session_token
from bp_router.security.passwords import needs_rehash, verify_password

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Models
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
# Helpers
# ---------------------------------------------------------------------------


def _hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/login", response_model=TokenPair)
async def login(req: LoginRequest, request: Request) -> TokenPair:
    state = request.app.state.bp
    settings = state.settings
    pool = state.db_pool

    async with pool.acquire() as conn:
        user = await queries.get_user_by_email(conn, req.email)
        if user is None or user.suspended_at is not None:
            await queries.append_audit_event(
                conn,
                actor_kind="user",
                actor_id=None,
                event="auth.login_failed",
                payload={"email": req.email, "reason": "no_user"},
            )
            raise HTTPException(status_code=401, detail="invalid credentials")

        if user.auth_kind != "password" or not user.auth_secret_hash:
            raise HTTPException(status_code=401, detail="invalid credentials")

        if not verify_password(req.password, user.auth_secret_hash):
            await queries.append_audit_event(
                conn,
                actor_kind="user",
                actor_id=user.user_id,
                event="auth.login_failed",
                payload={"reason": "bad_password"},
            )
            raise HTTPException(status_code=401, detail="invalid credentials")

        # TOTP would be enforced here; out of scope for the skeleton happy path.

        if needs_rehash(user.auth_secret_hash):
            from bp_router.security.passwords import hash_password  # noqa: PLC0415

            new_hash = hash_password(req.password)
            await conn.execute(
                "UPDATE users SET auth_secret_hash = $2 WHERE user_id = $1",
                user.user_id,
                new_hash,
            )

    access, expires_at, _jti = issue_session_token(
        user_id=user.user_id,
        role=user.role,
        user_tier=user.user_tier,
        secret=settings.jwt_secret.get_secret_value(),
        ttl_s=settings.session_jwt_ttl_s,
        key_version=settings.jwt_key_version,
        algorithm=settings.jwt_algorithm,
    )

    refresh = secrets.token_urlsafe(32)
    refresh_expires = _now() + timedelta(seconds=settings.refresh_token_ttl_s)

    async with pool.acquire() as conn:
        await queries.insert_refresh_token(
            conn,
            token_hash=_hash_refresh_token(refresh),
            user_id=user.user_id,
            expires_at=refresh_expires,
        )
        await queries.append_audit_event(
            conn,
            actor_kind="user",
            actor_id=user.user_id,
            event="auth.login_succeeded",
        )

    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        role=user.role,
        user_tier=user.user_tier,
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(req: RefreshRequest, request: Request) -> TokenPair:
    state = request.app.state.bp
    settings = state.settings
    pool = state.db_pool

    new_refresh = secrets.token_urlsafe(32)

    async with pool.acquire() as conn:
        async with conn.transaction():
            user_id = await queries.consume_refresh_token(
                conn,
                token_hash=_hash_refresh_token(req.refresh_token),
                replaced_by=_hash_refresh_token(new_refresh),
            )
            if user_id is None:
                await queries.append_audit_event(
                    conn,
                    actor_kind="user",
                    actor_id=None,
                    event="auth.refresh_replayed",
                )
                raise HTTPException(status_code=401, detail="invalid refresh token")

            user = await queries.get_user_by_id(conn, user_id)
            if user is None or user.suspended_at is not None:
                raise HTTPException(status_code=401, detail="user inactive")

            new_expires = _now() + timedelta(seconds=settings.refresh_token_ttl_s)
            await queries.insert_refresh_token(
                conn,
                token_hash=_hash_refresh_token(new_refresh),
                user_id=user.user_id,
                expires_at=new_expires,
            )

    access, expires_at, _jti = issue_session_token(
        user_id=user.user_id,
        role=user.role,
        user_tier=user.user_tier,
        secret=settings.jwt_secret.get_secret_value(),
        ttl_s=settings.session_jwt_ttl_s,
        key_version=settings.jwt_key_version,
        algorithm=settings.jwt_algorithm,
    )

    return TokenPair(
        access_token=access,
        refresh_token=new_refresh,
        expires_at=expires_at,
        role=user.role,
        user_tier=user.user_tier,
    )
