"""bp_sdk.onboarding — First-run agent registration and token refresh."""

from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional

import httpx

if TYPE_CHECKING:
    from bp_protocol.types import AgentInfo
    from bp_sdk.settings import AgentConfig

logger = logging.getLogger(__name__)


def _credentials_path(config: "AgentConfig"):  # type: ignore[no-untyped-def]
    return config.state_dir / "credentials.json"


def _onboard_http_url(config: "AgentConfig") -> str:
    if config.onboard_url:
        return config.onboard_url.rstrip("/")
    # Derive: ws[s]://host/v1/agent → http[s]://host
    url = config.router_url
    if url.startswith("wss://"):
        url = "https://" + url[len("wss://"):]
    elif url.startswith("ws://"):
        url = "http://" + url[len("ws://"):]
    if "/v1/" in url:
        url = url.split("/v1/")[0]
    return url.rstrip("/")


def _persist_credentials(
    config: "AgentConfig",
    *,
    agent_id: str,
    auth_token: str,
    expires_at: Optional[str],
) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    creds_path = _credentials_path(config)
    creds_path.write_text(
        json.dumps(
            {
                "agent_id": agent_id,
                "auth_token": auth_token,
                "expires_at": expires_at,
            },
            indent=2,
        )
    )
    try:
        creds_path.chmod(0o600)
    except Exception:  # noqa: BLE001
        pass


async def onboard_or_resume(info: "AgentInfo", config: "AgentConfig") -> None:
    """Ensure config.auth_token is set. Persists creds across restarts.

    1. If `state_dir/credentials.json` exists with a valid token, load it.
    2. Else perform `POST /v1/onboard` using `invitation_token`.
    3. Persist the result with permissions 0600.
    """
    creds_path = _credentials_path(config)
    if creds_path.exists():
        try:
            data = json.loads(creds_path.read_text())
            token = data.get("auth_token")
            expires_at = data.get("expires_at")
            if token and (expires_at is None or _is_future(expires_at)):
                config.auth_token = token
                return
        except Exception:  # noqa: BLE001
            logger.exception("credentials_load_failed")

    if not config.invitation_token:
        raise RuntimeError(
            "Agent has no auth_token and no invitation_token. Set "
            "AGENT_INVITATION_TOKEN to the token issued by the router admin."
        )

    base = _onboard_http_url(config)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base}/v1/onboard",
            json={
                "invitation_token": config.invitation_token,
                "agent_info": info.model_dump(),
            },
        )
        resp.raise_for_status()
        data = resp.json()

    config.auth_token = data["auth_token"]
    _persist_credentials(
        config,
        agent_id=data["agent_id"],
        auth_token=data["auth_token"],
        expires_at=data.get("expires_at"),
    )


def _is_future(iso: str) -> bool:
    try:
        return datetime.fromisoformat(iso) > datetime.utcnow()
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


def decode_token_exp(token: str) -> Optional[int]:
    """Read the `exp` claim from a JWT without verifying the signature.

    The SDK doesn't have the signing secret — it only needs the timing to
    schedule a proactive refresh. PyJWT would also accept
    `options={"verify_signature": False}`, but doing it by hand keeps
    the SDK's runtime dep set lean.

    Returns the unix-seconds expiry, or None if the token is malformed.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except Exception:  # noqa: BLE001
        return None


async def refresh_token(config: "AgentConfig") -> Optional[int]:
    """Rotate the agent's bearer token via POST /v1/agent/refresh-token.

    Updates `config.auth_token` and persists credentials.json on success.
    Returns the new expiry (unix seconds) or None on failure. Callers
    treat None as "transient — retry shortly".
    """
    token = config.auth_token
    if not token:
        return None

    creds_path = _credentials_path(config)
    agent_id = None
    if creds_path.exists():
        try:
            agent_id = json.loads(creds_path.read_text()).get("agent_id")
        except Exception:  # noqa: BLE001
            pass
    if agent_id is None:
        # Fall back to decoding the JWT's `sub` claim for agent_id.
        try:
            parts = token.split(".")
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            agent_id = payload.get("sub")
        except Exception:  # noqa: BLE001
            logger.warning(
                "refresh_token_no_agent_id",
                extra={"event": "refresh_token_no_agent_id"},
            )
            return None
    if agent_id is None:
        return None

    base = _onboard_http_url(config)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base}/v1/agent/refresh-token",
                json={"agent_id": agent_id},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning(
            "refresh_token_http_failed",
            extra={"event": "refresh_token_http_failed", "error": repr(exc)},
        )
        return None

    new_token = data.get("auth_token")
    new_expires_at = data.get("expires_at")
    if not new_token:
        return None

    config.auth_token = new_token
    _persist_credentials(
        config,
        agent_id=agent_id,
        auth_token=new_token,
        expires_at=new_expires_at,
    )
    new_exp = decode_token_exp(new_token)
    logger.info(
        "agent_token_refreshed",
        extra={
            "event": "agent_token_refreshed",
            "bp.agent_id": agent_id,
            "expires_at": new_expires_at,
        },
    )
    return new_exp


def schedule_seconds_until_refresh(token: str, *, min_buffer_s: int = 60) -> float:
    """How long to wait before refreshing `token`.

    Refreshes at exp - max(min_buffer_s, ttl/10) so that the buffer scales
    with token lifetime (long-lived → refresh well before expiry; short-
    lived → refresh closer to expiry but still with a sensible margin).

    Returns 0 when the token is already past refresh, or a small positive
    value when the buffer is bigger than the remaining lifetime.
    """
    exp = decode_token_exp(token)
    if exp is None:
        return 300.0  # unknown — recheck later
    now = time.time()
    remaining = exp - now
    if remaining <= 0:
        return 0.0
    buffer = max(min_buffer_s, remaining / 10)
    return max(0.0, remaining - buffer)
