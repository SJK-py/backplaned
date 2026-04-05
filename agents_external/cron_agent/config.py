"""
cron_agent/config.py — Configuration for the Cron Agent.
"""

from __future__ import annotations

import os

from pydantic import BaseModel


class AgentConfig(BaseModel):
    router_url: str = "http://localhost:8000"
    invitation_token: str = ""
    receive_url: str = "http://localhost:8085/receive"

    agent_host: str = "0.0.0.0"
    agent_port: int = 8085
    agent_id: str = "cron_agent"
    agent_auth_token: str = ""
    agent_endpoint_url: str = ""
    admin_password: str = ""
    session_secret: str = ""

    check_interval: int = 30  # seconds between scheduler ticks
    llm_agent_id: str = "llm_agent"
    core_agent_id: str = "core_personal_agent"
    default_model_id: str = ""
    tool_timeout: int = 120

    data_dir: str = "data"
    log_dir: str = "data/logs"

    @classmethod
    def from_env(cls) -> AgentConfig:
        import secrets as _s
        return cls(
            router_url=os.environ.get("ROUTER_URL", cls.model_fields["router_url"].default),
            invitation_token=os.environ.get("INVITATION_TOKEN", ""),
            receive_url=os.environ.get("RECEIVE_URL", cls.model_fields["receive_url"].default),
            agent_host=os.environ.get("AGENT_HOST", cls.model_fields["agent_host"].default),
            agent_port=int(os.environ.get("AGENT_PORT", cls.model_fields["agent_port"].default)),
            agent_id=os.environ.get("AGENT_ID", cls.model_fields["agent_id"].default),
            agent_endpoint_url=os.environ.get("AGENT_ENDPOINT_URL", cls.model_fields["agent_endpoint_url"].default),
            admin_password=os.environ.get("ADMIN_PASSWORD", cls.model_fields["admin_password"].default),
            session_secret=os.environ.get("SESSION_SECRET") or _s.token_hex(32),
            check_interval=int(os.environ.get("CHECK_INTERVAL", cls.model_fields["check_interval"].default)),
            llm_agent_id=os.environ.get("LLM_AGENT_ID", cls.model_fields["llm_agent_id"].default),
            core_agent_id=os.environ.get("CORE_AGENT_ID", cls.model_fields["core_agent_id"].default),
            default_model_id=os.environ.get("DEFAULT_MODEL_ID", ""),
            tool_timeout=int(os.environ.get("TOOL_TIMEOUT", cls.model_fields["tool_timeout"].default)),
            data_dir=os.environ.get("DATA_DIR", cls.model_fields["data_dir"].default),
            log_dir=os.environ.get("LOG_DIR", cls.model_fields["log_dir"].default),
            agent_auth_token=os.environ.get("AGENT_AUTH_TOKEN", cls.model_fields["agent_auth_token"].default),
        )
