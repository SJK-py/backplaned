"""bp_sdk.onboarding — First-run agent registration."""

from __future__ import annotations

import json
import logging
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
    config.state_dir.mkdir(parents=True, exist_ok=True)
    creds_path.write_text(
        json.dumps(
            {
                "agent_id": data["agent_id"],
                "auth_token": data["auth_token"],
                "expires_at": data.get("expires_at"),
            },
            indent=2,
        )
    )
    try:
        creds_path.chmod(0o600)
    except Exception:  # noqa: BLE001
        pass


def _is_future(iso: str) -> bool:
    try:
        return datetime.fromisoformat(iso) > datetime.utcnow()
    except Exception:  # noqa: BLE001
        return False


async def refresh_token(config: "AgentConfig") -> Optional[str]:
    """Rotate the agent's token via /v1/agent/refresh-token. Returns new token."""
    raise NotImplementedError
