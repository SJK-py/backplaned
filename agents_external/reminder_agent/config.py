"""
reminder_agent/config.py — Configuration for the Reminder Agent.

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
    agent_id: str = "reminder_agent"
    agent_auth_token: str = ""
    invitation_token: str = ""
    agent_host: str = "0.0.0.0"
    agent_port: int = 8101
    agent_url: str = ""
    data_dir: str = "data"
    log_dir: str = "data/logs"
    credentials_path: str = "data/credentials.json"
    admin_password: str = ""
    session_secret: str = ""

    # --- Runtime (from data/config.json) ---
    llm_timeout: int = 120
    tool_timeout: int = 60
    check_interval: int = 30
    event_notify_hours: str = "0,3"
    task_notify_hours: str = "0,3,72"
    urgent_task_notify_hours: str = "0,1,2,4,8,12,24,36,48,72"
    core_agent_id: str = "core_personal_agent"
    llm_agent_id: str = "llm_agent"

    @classmethod
    def from_env(cls) -> AgentConfig:
        """Load configuration from .env + data/config.json."""
        import secrets as _s
        cfg = _load_config()

        def _get(key: str, env_key: str | None = None, default: Any = None) -> Any:
            """config.json > env var > default. Handles zero/false correctly."""
            v = cfg.get(key)
            if v is not None:
                return v
            e = os.environ.get(env_key or key)
            if e is not None and e != "":
                return e
            return default

        return cls(
            # Infrastructure (env only)
            router_url=os.environ.get("ROUTER_URL", cls.model_fields["router_url"].default),
            agent_id=os.environ.get("AGENT_ID", cls.model_fields["agent_id"].default),
            agent_auth_token=os.environ.get("AGENT_AUTH_TOKEN", cls.model_fields["agent_auth_token"].default),
            invitation_token=os.environ.get("INVITATION_TOKEN", cls.model_fields["invitation_token"].default),
            agent_host=os.environ.get("AGENT_HOST", cls.model_fields["agent_host"].default),
            agent_port=int(os.environ.get("AGENT_PORT", cls.model_fields["agent_port"].default)),
            agent_url=os.environ.get("AGENT_URL", ""),
            data_dir=os.environ.get("DATA_DIR", cls.model_fields["data_dir"].default),
            log_dir=os.environ.get("LOG_DIR", cls.model_fields["log_dir"].default),
            credentials_path=os.environ.get("CREDENTIALS_PATH", cls.model_fields["credentials_path"].default),
            admin_password=os.environ.get("ADMIN_PASSWORD", cls.model_fields["admin_password"].default),
            session_secret=os.environ.get("SESSION_SECRET") or _s.token_hex(32),
            # Runtime (config.json > env fallback > default)
            llm_timeout=int(_get("LLM_TIMEOUT", default=cls.model_fields["llm_timeout"].default)),
            tool_timeout=int(_get("TOOL_TIMEOUT", default=cls.model_fields["tool_timeout"].default)),
            check_interval=int(_get("CHECK_INTERVAL", default=cls.model_fields["check_interval"].default)),
            event_notify_hours=str(_get("EVENT_NOTIFY_HOURS", default=cls.model_fields["event_notify_hours"].default)),
            task_notify_hours=str(_get("TASK_NOTIFY_HOURS", default=cls.model_fields["task_notify_hours"].default)),
            urgent_task_notify_hours=str(_get("URGENT_TASK_NOTIFY_HOURS", default=cls.model_fields["urgent_task_notify_hours"].default)),
            core_agent_id=_get("CORE_AGENT_ID", default=cls.model_fields["core_agent_id"].default),
            llm_agent_id=_get("LLM_AGENT_ID", default=cls.model_fields["llm_agent_id"].default),
        )
