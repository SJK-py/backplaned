"""bp_router.security.secrets — secret_ref resolver.

Settings can reference values stored in a secrets backend rather than
inlining them. Supported schemes:

  env://VAR_NAME            — read from environment variable
  vault://kv/path/to/key    — HashiCorp Vault KV (deferred import)
  awssm://name              — AWS Secrets Manager (deferred import)
  gcpsm://projects/.../...  — GCP Secret Manager (deferred import)

See `docs/design/security.md` §6.3.
"""

from __future__ import annotations

import os
from typing import Optional


def resolve_secret_ref(value: str) -> str:
    """Resolve a `secret_ref` URL to its plaintext value.

    Returns the input unchanged if it does not look like a secret_ref —
    so callers can pass either a raw secret or a reference.
    """
    if "://" not in value:
        return value
    scheme, _, rest = value.partition("://")
    scheme = scheme.lower()

    if scheme == "env":
        v = os.environ.get(rest)
        if v is None:
            raise KeyError(f"secret_ref env://{rest} not set")
        return v
    if scheme == "vault":
        return _resolve_vault(rest)
    if scheme == "awssm":
        return _resolve_aws_sm(rest)
    if scheme == "gcpsm":
        return _resolve_gcp_sm(rest)

    raise ValueError(f"Unsupported secret_ref scheme: {scheme!r}")


def _resolve_vault(path: str) -> str:
    raise NotImplementedError("vault:// resolver pending — install hvac and implement")


def _resolve_aws_sm(name: str) -> str:
    raise NotImplementedError("awssm:// resolver pending — install boto3 and implement")


def _resolve_gcp_sm(name: str) -> str:
    raise NotImplementedError(
        "gcpsm:// resolver pending — install google-cloud-secret-manager and implement"
    )


# ---------------------------------------------------------------------------
# Caching wrapper
# ---------------------------------------------------------------------------


class SecretCache:
    """In-memory cache for resolved secrets with TTL.

    Provider API keys and DB passwords are read on every request hot
    path; resolving them through `resolve_secret_ref` for each call
    is wasteful. The cache holds resolved values for `ttl_s` seconds.
    """

    def __init__(self, *, ttl_s: int = 300) -> None:
        self._ttl_s = ttl_s
        self._cache: dict[str, tuple[str, float]] = {}

    def get(self, ref: str) -> str:
        import time  # noqa: PLC0415

        now = time.monotonic()
        cached = self._cache.get(ref)
        if cached is not None and now - cached[1] < self._ttl_s:
            return cached[0]
        value = resolve_secret_ref(ref)
        self._cache[ref] = (value, now)
        return value

    def invalidate(self, ref: Optional[str] = None) -> None:
        if ref is None:
            self._cache.clear()
        else:
            self._cache.pop(ref, None)
