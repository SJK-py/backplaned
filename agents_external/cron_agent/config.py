"""
cron_agent/config.py — Configuration for the Cron Agent.

Infrastructure settings (secrets, URLs, ports) come from environment
variables / .env.  Runtime settings (timeouts, model IDs, intervals)
come from data/config.json and are editable via the web admin UI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_CONFIG_PATH = Path(__file__).resolve().parent / "data" / "config.json"


def _load_config() -> dict[str, Any]:
    """Read data/config.json (re-read on every call for hot-reload)."""
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


class AgentConfig(BaseModel):
    """Agent-level configuration."""

    # --- Infrastructure (from .env) ---
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
    data_dir: str = "data"
    log_dir: str = "data/logs"

    # --- Runtime (from data/config.json) ---
    check_interval: int = 30
    llm_agent_id: str = "llm_agent"
    core_agent_id: str = "core_personal_agent"
    default_model_id: str = ""
    tool_timeout: int = 120

    @classmethod
    def from_env(cls) -> AgentConfig:
        import secrets as _s
        cfg = _load_config()
        return cls(
            # Infrastructure (env only)
            router_url=os.environ.get("ROUTER_URL", cls.model_fields["router_url"].default),
            invitation_token=os.environ.get("INVITATION_TOKEN", ""),
            receive_url=os.environ.get("RECEIVE_URL", cls.model_fields["receive_url"].default),
            agent_host=os.environ.get("AGENT_HOST", cls.model_fields["agent_host"].default),
            agent_port=int(os.environ.get("AGENT_PORT", cls.model_fields["agent_port"].default)),
            agent_id=os.environ.get("AGENT_ID", cls.model_fields["agent_id"].default),
            agent_endpoint_url=os.environ.get("AGENT_ENDPOINT_URL", cls.model_fields["agent_endpoint_url"].default),
            admin_password=os.environ.get("ADMIN_PASSWORD", cls.model_fields["admin_password"].default),
            session_secret=os.environ.get("SESSION_SECRET") or _s.token_hex(32),
            agent_auth_token=os.environ.get("AGENT_AUTH_TOKEN", cls.model_fields["agent_auth_token"].default),
            data_dir=os.environ.get("DATA_DIR", cls.model_fields["data_dir"].default),
            log_dir=os.environ.get("LOG_DIR", cls.model_fields["log_dir"].default),
            # Runtime (config.json > env fallback > default)
            check_interval=int(cfg.get("CHECK_INTERVAL") or os.environ.get("CHECK_INTERVAL") or cls.model_fields["check_interval"].default),
            llm_agent_id=cfg.get("LLM_AGENT_ID") or os.environ.get("LLM_AGENT_ID") or cls.model_fields["llm_agent_id"].default,
            core_agent_id=cfg.get("CORE_AGENT_ID") or os.environ.get("CORE_AGENT_ID") or cls.model_fields["core_agent_id"].default,
            default_model_id=cfg.get("DEFAULT_MODEL_ID") or os.environ.get("DEFAULT_MODEL_ID", ""),
            tool_timeout=int(cfg.get("TOOL_TIMEOUT") or os.environ.get("TOOL_TIMEOUT") or cls.model_fields["tool_timeout"].default),
        )
