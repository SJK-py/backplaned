"""
coding_agent/config.py — Configuration loading for the Coding Agent.

Infrastructure settings (secrets, URLs, ports) come from environment
variables / .env.  Runtime settings (timeouts, model IDs) come from
data/config.json.  Per-user settings (workspace limits, allowed
commands) are in a separate per-user config file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_CONFIG_PATH = Path(__file__).resolve().parent / "data" / "config.json"


def _load_config() -> dict[str, Any]:
    """Read data/config.json (re-read on every call for hot-reload)."""
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Per-user configuration (from user_config.json)
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
# Agent-level configuration
# ---------------------------------------------------------------------------

class AgentConfig(BaseModel):
    """Agent-level configuration."""

    # --- Infrastructure (from .env) ---
    router_url: str = "http://localhost:8000"
    agent_id: str = "coding_agent"
    agent_auth_token: str = ""
    invitation_token: str = ""
    agent_host: str = "0.0.0.0"
    agent_port: int = 8100
    agent_url: str = ""
    data_dir: str = "data"
    log_dir: str = "data/logs"
    admin_password: str = ""
    session_secret: str = ""

    # --- Runtime (from data/config.json) ---
    llm_timeout: int = 120
    tool_timeout: int = 60
    workspace_root: str = "data/workspaces"
    user_config_path: str = "data/user_config.json"
    llm_agent_id: str = "llm_agent"
    default_model_id: str = ""

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
            admin_password=os.environ.get("ADMIN_PASSWORD", cls.model_fields["admin_password"].default),
            session_secret=os.environ.get("SESSION_SECRET") or _s.token_hex(32),
            # Runtime (config.json > env fallback > default)
            llm_timeout=int(_get("LLM_TIMEOUT", default=cls.model_fields["llm_timeout"].default)),
            tool_timeout=int(_get("TOOL_TIMEOUT", default=cls.model_fields["tool_timeout"].default)),
            workspace_root=_get("WORKSPACE_ROOT", default=cls.model_fields["workspace_root"].default),
            user_config_path=_get("USER_CONFIG_PATH", default=cls.model_fields["user_config_path"].default),
            llm_agent_id=_get("LLM_AGENT_ID", default=cls.model_fields["llm_agent_id"].default),
            default_model_id=_get("DEFAULT_MODEL_ID", default=""),
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
