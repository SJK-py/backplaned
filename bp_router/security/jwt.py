"""bp_router.security.jwt — JWT issuance, verification, and FastAPI deps.

See `docs/design/security.md` §3-5.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import Depends, HTTPException, Request

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
    raise NotImplementedError


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
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_token(
    token: str,
    *,
    secret: str,
    expected_kind: Literal["session", "agent"],
    revoked_jti: Optional[set[str]] = None,
    key_version: int,
    algorithm: str = "HS256",
) -> dict[str, Any]:
    """Verify signature, expiry, kind claim, and revocation. Returns claims."""
    raise NotImplementedError


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
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=401, detail="invalid token")
    return SessionPrincipal(
        user_id=claims["sub"],
        role=claims["role"],
        user_tier=claims.get("user_tier", "free"),
        expires_at=datetime.fromtimestamp(claims["exp"]),
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
        # Cheap SMEMBERS read; the set is small (recently-revoked jti only).
        members = await state.redis.smembers("router:revoked_jti")
        return set(members) if members else set()
    return set()
