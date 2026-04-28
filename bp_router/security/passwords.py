"""bp_router.security.passwords — argon2id password hashing.

OWASP-tuned defaults; parameters configurable via deployment settings
if needed. See `docs/security.md` §3.1.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

# Defaults follow OWASP 2024 recommendations for argon2id.
_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,  # 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


def hash_password(plaintext: str) -> str:
    """Return an encoded argon2id hash. Constant-time on verification."""
    return _HASHER.hash(plaintext)


def verify_password(plaintext: str, encoded: str) -> bool:
    """Verify a plaintext against a stored hash.

    Returns False on mismatch (rather than raising) so callers can
    branch cleanly. Returns False also on malformed `encoded` to avoid
    an information leak.
    """
    try:
        return _HASHER.verify(encoded, plaintext)
    except VerifyMismatchError:
        return False
    except Exception:  # noqa: BLE001
        return False


def needs_rehash(encoded: str) -> bool:
    """True if argon2 parameters have changed since this hash was made."""
    try:
        return _HASHER.check_needs_rehash(encoded)
    except Exception:  # noqa: BLE001
        return False
