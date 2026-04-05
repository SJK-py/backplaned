"""
reminder_agent/config.py — Configuration for the Reminder Agent.

Loads agent-level settings from environment variables.
"""

from __future__ import annotations

import os

from pydantic import BaseModel


class AgentConfig(BaseModel):
    """Agent-level configuration sourced from environment variables."""

    # Timeouts
    llm_timeout: int = 120
    tool_timeout: int = 60

    # Router
    router_url: str = "http://localhost:8000"
    agent_id: str = "reminder_agent"
    agent_auth_token: str = ""
    invitation_token: str = ""

    # Server
    agent_host: str = "0.0.0.0"
    agent_port: int = 8101
    agent_endpoint_url: str = ""

    # Paths
    data_dir: str = "data"
    log_dir: str = "data/logs"
    credentials_path: str = "data/credentials.json"

    # Periodic checker
    check_interval: int = 30          # minutes
    check_lookahead_hours: float = 72  # how far ahead to check events (3 days)

    # Notification target
    core_agent_id: str = "core_personal_agent"

    # LLM Agent
    llm_agent_id: str = "llm_agent"

    # Web UI
    admin_password: str = ""
    session_secret: str = ""

    @classmethod
    def from_env(cls) -> AgentConfig:
        """Load configuration from environment variables."""
        import secrets as _s
        return cls(
            llm_timeout=int(os.environ.get("LLM_TIMEOUT", cls.model_fields["llm_timeout"].default)),
            tool_timeout=int(os.environ.get("TOOL_TIMEOUT", cls.model_fields["tool_timeout"].default)),
            router_url=os.environ.get("ROUTER_URL", cls.model_fields["router_url"].default),
            agent_id=os.environ.get("AGENT_ID", cls.model_fields["agent_id"].default),
            agent_auth_token=os.environ.get("AGENT_AUTH_TOKEN", cls.model_fields["agent_auth_token"].default),
            invitation_token=os.environ.get("INVITATION_TOKEN", cls.model_fields["invitation_token"].default),
            agent_host=os.environ.get("AGENT_HOST", cls.model_fields["agent_host"].default),
            agent_port=int(os.environ.get("AGENT_PORT", cls.model_fields["agent_port"].default)),
            agent_endpoint_url=os.environ.get("AGENT_ENDPOINT_URL", ""),
            data_dir=os.environ.get("DATA_DIR", cls.model_fields["data_dir"].default),
            log_dir=os.environ.get("LOG_DIR", cls.model_fields["log_dir"].default),
            credentials_path=os.environ.get("CREDENTIALS_PATH", cls.model_fields["credentials_path"].default),
            check_interval=int(os.environ.get("CHECK_INTERVAL", cls.model_fields["check_interval"].default)),
            check_lookahead_hours=float(os.environ.get("CHECK_LOOKAHEAD_HOURS", cls.model_fields["check_lookahead_hours"].default)),
            core_agent_id=os.environ.get("CORE_AGENT_ID", cls.model_fields["core_agent_id"].default),
            llm_agent_id=os.environ.get("LLM_AGENT_ID", cls.model_fields["llm_agent_id"].default),
            admin_password=os.environ.get("ADMIN_PASSWORD", cls.model_fields["admin_password"].default),
            session_secret=os.environ.get("SESSION_SECRET") or _s.token_hex(32),
        )
