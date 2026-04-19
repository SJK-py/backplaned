"""
reminder_agent/oauth.py — Google OAuth 2.0 helpers.

Builds the authorization URL, exchanges the callback ``code`` for tokens,
revokes refresh tokens, and fetches user-info (email).  All operations are
synchronous (the underlying libraries are sync-only); callers should wrap
them in ``asyncio.to_thread`` when used from async contexts.
"""

from __future__ import annotations

import logging
from datetime import timezone
from typing import Any

import httpx

from sync import GOOGLE_SCOPES, GOOGLE_TOKEN_URI

logger = logging.getLogger("reminder_agent.oauth")

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_REVOKE_URI = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_URI = "https://www.googleapis.com/oauth2/v2/userinfo"


def _client_config(client_id: str, client_secret: str, redirect_uri: str) -> dict[str, Any]:
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": GOOGLE_AUTH_URI,
            "token_uri": GOOGLE_TOKEN_URI,
            "redirect_uris": [redirect_uri],
        }
    }


def build_authorization_url(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    state: str,
) -> str:
    """Return the Google OAuth consent URL for the given signed state."""
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        _client_config(client_id, client_secret, redirect_uri),
        scopes=GOOGLE_SCOPES,
        redirect_uri=redirect_uri,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
        state=state,
    )
    return auth_url


def exchange_code(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
) -> dict[str, Any]:
    """Exchange an authorization code for tokens.

    Returns a dict with ``access_token``, ``refresh_token``, and
    ``token_expires_at`` (ISO8601 UTC).  ``refresh_token`` may be missing
    if the user previously authorized this client and Google didn't reissue
    one — the caller should handle that case (typically by re-prompting
    consent with ``prompt=consent`` which we already pass).
    """
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        _client_config(client_id, client_secret, redirect_uri),
        scopes=GOOGLE_SCOPES,
        redirect_uri=redirect_uri,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials

    expires_at = None
    if creds.expiry is not None:
        expires_at = creds.expiry.replace(tzinfo=timezone.utc).isoformat()

    return {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_expires_at": expires_at,
    }


def revoke_token(token: str) -> bool:
    """Revoke the given refresh or access token. Returns True on success."""
    if not token:
        return False
    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.post(
                GOOGLE_REVOKE_URI,
                params={"token": token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if r.status_code == 200:
            return True
        logger.warning("Token revoke returned %d: %s", r.status_code, r.text)
        return False
    except Exception:
        logger.exception("Token revoke failed")
        return False


def fetch_userinfo(access_token: str) -> dict[str, Any]:
    """Return Google's userinfo dict (id, email, verified_email, name, ...)."""
    with httpx.Client(timeout=10.0) as c:
        r = c.get(
            GOOGLE_USERINFO_URI,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        r.raise_for_status()
        return r.json()
