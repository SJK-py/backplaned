"""bp_router.security — Auth, secrets, password hashing.

See `docs/design/security.md`.
"""

from bp_router.security.jwt import (
    SessionPrincipal,
    issue_agent_token,
    issue_session_token,
    require_admin,
    require_user,
    verify_token,
)
from bp_router.security.passwords import hash_password, verify_password
from bp_router.security.secrets import resolve_secret_ref

__all__ = [
    "SessionPrincipal",
    "hash_password",
    "issue_agent_token",
    "issue_session_token",
    "require_admin",
    "require_user",
    "resolve_secret_ref",
    "verify_password",
    "verify_token",
]
