"""bp_router.security.jwt — JWT issuance, verification, and FastAPI deps.

See `docs/security.md` §3-5.
"""

from __future__ import annotations

import secrets as _secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

import jwt as pyjwt
from fastapi import Depends, HTTPException, Request

ISSUER = "bp_router"


# ---------------------------------------------------------------------------
# Principal types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionPrincipal:
    """Decoded session JWT — what require_user / require_admin yield."""

    user_id: str
    role: Literal["admin", "user", "service"]
    user_tier: str
    expires_at: datetime
    jti: str


@dataclass(frozen=True)
class AgentPrincipal:
    """Decoded agent JWT — used by the WebSocket Hello validator."""

    agent_id: str
    expires_at: datetime
    jti: str
    sdk_protocol_version: str


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_jti() -> str:
    return _secrets.token_urlsafe(16)


def issue_session_token(
    *,
    user_id: str,
    role: str,
    user_tier: str,
    secret: str,
    ttl_s: int,
    key_version: int,
    algorithm: str = "HS256",
) -> tuple[str, datetime, str]:
    """Returns (token, expires_at, jti)."""
    iat = _now()
    exp = iat + timedelta(seconds=ttl_s)
    jti = _new_jti()
    claims = {
        "iss": ISSUER,
        "sub": user_id,
        "iat": int(iat.timestamp()),
        "exp": int(exp.timestamp()),
        "kind": "session",
        "role": role,
        "user_tier": user_tier,
        "kver": key_version,
        "jti": jti,
    }
    token = pyjwt.encode(claims, secret, algorithm=algorithm)
    return token, exp, jti


def issue_agent_token(
    *,
    agent_id: str,
    secret: str,
    ttl_s: int,
    key_version: int,
    protocol_version: str,
    algorithm: str = "HS256",
) -> tuple[str, datetime, str]:
    """Returns (token, expires_at, jti)."""
    iat = _now()
    exp = iat + timedelta(seconds=ttl_s)
    jti = _new_jti()
    claims = {
        "iss": ISSUER,
        "sub": agent_id,
        "iat": int(iat.timestamp()),
        "exp": int(exp.timestamp()),
        "kind": "agent",
        "ver": protocol_version,
        "kver": key_version,
        "jti": jti,
    }
    token = pyjwt.encode(claims, secret, algorithm=algorithm)
    return token, exp, jti


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class TokenError(Exception):
    """Generic verification failure — message is safe to surface to callers."""


def verify_token(
    token: str,
    *,
    secret: str,
    expected_kind: Literal["session", "agent"],
    revoked_jti: Optional[set[str]] = None,
    key_version: int,
    algorithm: str = "HS256",
) -> dict[str, Any]:
    """Verify signature, expiry, kind claim, and revocation. Returns claims.

    Raises TokenError on any verification failure. Callers should treat
    all such failures uniformly (e.g. "invalid token") to avoid
    information leaks.
    """
    try:
        claims = pyjwt.decode(
            token,
            secret,
            algorithms=[algorithm],
            issuer=ISSUER,
            options={"require": ["exp", "iat", "iss", "sub", "kind", "jti", "kver"]},
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise TokenError("expired") from exc
    except pyjwt.InvalidTokenError as exc:
        raise TokenError(f"invalid: {exc}") from exc

    if claims.get("kind") != expected_kind:
        raise TokenError("wrong_kind")
    if int(claims.get("kver", 0)) != key_version:
        raise TokenError("stale_key_version")
    if revoked_jti is not None and claims.get("jti") in revoked_jti:
        raise TokenError("revoked")
    return claims


def verify_agent_token(
    token: str,
    *,
    secret: str,
    revoked_jti: Optional[set[str]] = None,
    key_version: int,
    algorithm: str = "HS256",
) -> AgentPrincipal:
    """Specialised wrapper that returns an AgentPrincipal."""
    claims = verify_token(
        token,
        secret=secret,
        expected_kind="agent",
        revoked_jti=revoked_jti,
        key_version=key_version,
        algorithm=algorithm,
    )
    return AgentPrincipal(
        agent_id=claims["sub"],
        expires_at=datetime.fromtimestamp(claims["exp"], tz=timezone.utc),
        jti=claims["jti"],
        sdk_protocol_version=str(claims.get("ver", "1")),
    )


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


async def _principal_from_request(request: Request) -> SessionPrincipal:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = auth[len("bearer "):].strip()
    settings = request.app.state.bp.settings
    revoked = await _load_revoked_jti(request)
    try:
        claims = verify_token(
            token,
            secret=settings.jwt_secret.get_secret_value(),
            expected_kind="session",
            revoked_jti=revoked,
            key_version=settings.jwt_key_version,
            algorithm=settings.jwt_algorithm,
        )
    except TokenError:
        raise HTTPException(status_code=401, detail="invalid token")
    return SessionPrincipal(
        user_id=claims["sub"],
        role=claims["role"],
        user_tier=claims.get("user_tier", "free"),
        expires_at=datetime.fromtimestamp(claims["exp"], tz=timezone.utc),
        jti=claims["jti"],
    )


async def require_user(
    principal: SessionPrincipal = Depends(_principal_from_request),
) -> SessionPrincipal:
    if principal.role not in {"user", "admin", "service"}:
        raise HTTPException(status_code=403, detail="forbidden")
    return principal


async def require_admin(
    principal: SessionPrincipal = Depends(_principal_from_request),
) -> SessionPrincipal:
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return principal


async def _load_revoked_jti(request: Request) -> set[str]:
    """Pull the revocation set from Redis (preferred) or DB."""
    state = request.app.state.bp
    if state.redis is not None:
        members = await state.redis.smembers("router:revoked_jti")
        return set(members) if members else set()
    return set()


# ---------------------------------------------------------------------------
# Revocation helpers
# ---------------------------------------------------------------------------


async def revoke_jti(redis: Any, jti: str, *, ttl_s: int) -> None:
    """Add a jti to the revocation set with TTL = its remaining lifetime.

    The set is sized by JWT TTL × revocation rate; in practice tiny.
    """
    if redis is None:
        return
    pipe = redis.pipeline()
    pipe.sadd("router:revoked_jti", jti)
    pipe.expire("router:revoked_jti", ttl_s)
    await pipe.execute()
