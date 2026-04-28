"""bp_router.settings — Pydantic Settings, validated at startup.

See `docs/router/storage.md` §5 for the configuration spec.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Single source of router configuration.

    Loaded from environment variables prefixed `ROUTER_`. Values may
    reference secrets stored in a backend via the `secret_ref` resolver
    (`bp_router.security.secrets`). Fail-fast on missing/invalid values.
    """

    model_config = SettingsConfigDict(
        env_prefix="ROUTER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Database / cache
    # ------------------------------------------------------------------

    db_url: str
    """Postgres DSN, e.g. `postgresql://user:pass@host:5432/db`."""

    db_pool_min_size: int = 1
    db_pool_max_size: int = 10
    db_statement_timeout_ms: int = 30_000

    redis_url: Optional[str] = None
    """Required for multi-worker deployments. Single-worker may omit."""

    # ------------------------------------------------------------------
    # File storage backend
    # ------------------------------------------------------------------

    file_store: Literal["local", "s3", "gcs", "r2"] = "local"
    file_store_options: dict[str, Any] = Field(default_factory=dict)
    """Backend-specific options (bucket, region, endpoint, etc.)."""

    file_default_ttl_s: int = 604_800  # 7 days

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    bind_host: str = "0.0.0.0"
    bind_port: int = 8000
    public_url: str
    """External base URL, e.g. `https://router.example.com`."""

    # ------------------------------------------------------------------
    # Auth / tokens
    # ------------------------------------------------------------------

    jwt_secret: SecretStr
    jwt_algorithm: Literal["HS256", "EdDSA"] = "HS256"
    jwt_key_version: int = 1
    session_jwt_ttl_s: int = 900       # 15 min
    refresh_token_ttl_s: int = 86_400  # 24 h
    agent_token_ttl_s: int = 86_400    # 24 h

    # ------------------------------------------------------------------
    # Protocol limits / runtime parameters
    # ------------------------------------------------------------------

    heartbeat_interval_ms: int = 20_000
    max_payload_bytes: int = 1_048_576
    per_socket_outbox_max: int = 256
    pending_ack_timeout_s: float = 30.0
    default_task_deadline_s: int = 300
    resume_window_s: int = 30

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    otel_endpoint: Optional[str] = None
    otel_service_name: str = "bp_router"
    log_level: str = "INFO"
    log_prompts: bool = False
    """Dev-only: include full prompts in logs. Rejected if env=prod."""

    deployment_env: Literal["dev", "staging", "prod"] = "dev"

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @field_validator("public_url")
    @classmethod
    def _public_url_must_be_absolute(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("public_url must be an absolute URL")
        return v.rstrip("/")

    @field_validator("log_prompts")
    @classmethod
    def _no_prompt_logging_in_prod(cls, v: bool, info: Any) -> bool:
        if v and info.data.get("deployment_env") == "prod":
            raise ValueError("log_prompts is not permitted in prod")
        return v


def load_settings() -> Settings:
    """Resolve `Settings` from env. Call once at process startup."""
    return Settings()  # type: ignore[call-arg]
