"""
coding_agent/config.py — Configuration loading for the Coding Agent.

Loads agent-level settings from environment variables and per-user
settings from a JSON config file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Per-user configuration (from config.json)
# ---------------------------------------------------------------------------

class UserConfig(BaseModel):
    """Per-user security and limit settings."""

    model_id: str = ""
    limit_to_workspace: bool = True
    allowed_paths: list[str] = Field(default_factory=list)
    blocked_commands: list[str] = Field(
        default_factory=lambda: ["rm -rf /", "shutdown", "reboot", "mkfs", "dd"]
    )
    allow_all_commands: bool = False
    allow_network: bool = True
    max_iterations: int = 20
    max_tool_calls: int = 50


# ---------------------------------------------------------------------------
# Agent-level configuration (from environment)
# ---------------------------------------------------------------------------

class AgentConfig(BaseModel):
    """Agent-level configuration sourced from environment variables."""

    # Timeouts
    llm_timeout: int = 120
    tool_timeout: int = 60

    # Workspace
    workspace_root: str = "/data/workspaces"

    # Router
    router_url: str = "http://localhost:8000"
    agent_id: str = "coding_agent"
    agent_auth_token: str = ""
    invitation_token: str = ""

    # Server
    agent_host: str = "0.0.0.0"
    agent_port: int = 8100
    agent_endpoint_url: str = ""

    # Paths
    data_dir: str = "data"
    user_config_path: str = "config.json"
    log_dir: str = "/data/logs"

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
            workspace_root=os.environ.get("WORKSPACE_ROOT", cls.model_fields["workspace_root"].default),
            router_url=os.environ.get("ROUTER_URL", cls.model_fields["router_url"].default),
            agent_id=os.environ.get("AGENT_ID", cls.model_fields["agent_id"].default),
            agent_auth_token=os.environ.get("AGENT_AUTH_TOKEN", cls.model_fields["agent_auth_token"].default),
            invitation_token=os.environ.get("INVITATION_TOKEN", cls.model_fields["invitation_token"].default),
            agent_host=os.environ.get("AGENT_HOST", cls.model_fields["agent_host"].default),
            agent_port=int(os.environ.get("AGENT_PORT", cls.model_fields["agent_port"].default)),
            agent_endpoint_url=os.environ.get("AGENT_ENDPOINT_URL", ""),
            data_dir=os.environ.get("DATA_DIR", cls.model_fields["data_dir"].default),
            user_config_path=os.environ.get("USER_CONFIG_PATH", cls.model_fields["user_config_path"].default),
            log_dir=os.environ.get("LOG_DIR", cls.model_fields["log_dir"].default),
            llm_agent_id=os.environ.get("LLM_AGENT_ID", cls.model_fields["llm_agent_id"].default),
            admin_password=os.environ.get("ADMIN_PASSWORD", cls.model_fields["admin_password"].default),
            session_secret=os.environ.get("SESSION_SECRET") or _s.token_hex(32),
        )


# ---------------------------------------------------------------------------
# Config file manager
# ---------------------------------------------------------------------------

class ConfigManager:
    """
    Manages per-user configuration backed by a JSON file.

    The JSON file is a dict keyed by user_id, each value being a UserConfig.
    Raises KeyError if a user_id is not found (no default fallback, to prevent
    accidental workspace sharing).
    """

    def __init__(self, config_path: str) -> None:
        self._path = Path(config_path)
        self._data: dict[str, dict[str, Any]] = {}
        self.reload()

    def reload(self) -> None:
        """Reload config from disk."""
        if self._path.exists():
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        else:
            self._data = {}
            self._save()

    def _save(self) -> None:
        """Persist current config to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    def get_user_config(self, user_id: str) -> UserConfig:
        """Get config for a registered user. Raises KeyError if not found."""
        raw = self._data.get(user_id)
        if raw is None:
            raise KeyError(f"User '{user_id}' is not registered")
        return UserConfig.model_validate(raw)

    def set_user_config(self, user_id: str, config: UserConfig) -> None:
        """Set config for a user and persist."""
        self._data[user_id] = config.model_dump()
        self._save()

    def delete_user(self, user_id: str) -> bool:
        """Delete a user's config. Returns True if user existed."""
        if user_id in self._data:
            del self._data[user_id]
            self._save()
            return True
        return False

    def list_users(self) -> list[str]:
        """Return all user_ids in config."""
        return list(self._data.keys())

    def get_raw(self) -> dict[str, dict[str, Any]]:
        """Return the full raw config dict."""
        return self._data.copy()
