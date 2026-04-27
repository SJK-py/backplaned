"""bp_sdk.settings — AgentConfig (env-driven)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentConfig(BaseSettings):
    """Per-process configuration for an external agent.

    Loaded from environment variables prefixed `AGENT_`. Embedded
    agents typically pass an `AgentConfig` constructed in Python
    instead of relying on env vars.
    """

    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    embedded: bool = False
    """When True, the agent runs in-process via InProcessTransport."""

    router_url: str = "ws://localhost:8000/v1/agent"
    """WebSocket URL for external agents."""

    state_dir: Path = Field(default_factory=lambda: Path("./agent_state"))
    """Directory for persisted credentials, inbox files, etc."""

    auth_token: Optional[str] = None
    """Bearer token. If absent at startup the SDK runs onboarding
    using `invitation_token`."""

    invitation_token: Optional[str] = None

    onboard_url: Optional[str] = None
    """HTTP base URL for onboarding (defaults derived from router_url)."""

    pending_results_timeout_s: float = 60.0
    pending_acks_timeout_s: float = 30.0
    progress_buffer_size: int = 256
    reconnect_initial_backoff_s: float = 0.5
    reconnect_max_backoff_s: float = 30.0

    log_level: str = "INFO"


def load_agent_config() -> AgentConfig:
    return AgentConfig()  # type: ignore[call-arg]
