"""webapp_agent/config.py — Configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_CONFIG_PATH = Path(__file__).resolve().parent / "data" / "config.json"


def _load_config() -> dict[str, Any]:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


class AgentConfig(BaseModel):
    router_url: str = "http://localhost:8000"
    agent_id: str = "webapp_agent"
    agent_auth_token: str = ""
    invitation_token: str = ""
    agent_host: str = "0.0.0.0"
    agent_port: int = 8090
    agent_url: str = ""
    data_dir: str = "data"
    credentials_path: str = "data/credentials.json"
    session_secret: str = ""
    channel_agent_url: str = "http://localhost:8081"

    session_title_delay_sec: int = 5
    archive_refresh_interval_sec: int = 60
    agents_refresh_interval_sec: int = 60

    @classmethod
    def from_env(cls) -> AgentConfig:
        import secrets as _s
        cfg = _load_config()

        def _get(key: str, default: Any = None) -> Any:
            v = cfg.get(key)
            if v is not None:
                return v
            e = os.environ.get(key)
            if e is not None and e != "":
                return e
            return default

        return cls(
            router_url=os.environ.get("ROUTER_URL", cls.model_fields["router_url"].default),
            agent_id=os.environ.get("AGENT_ID", cls.model_fields["agent_id"].default),
            agent_auth_token=os.environ.get("AGENT_AUTH_TOKEN", cls.model_fields["agent_auth_token"].default),
            invitation_token=os.environ.get("INVITATION_TOKEN", cls.model_fields["invitation_token"].default),
            agent_host=os.environ.get("AGENT_HOST", cls.model_fields["agent_host"].default),
            agent_port=int(os.environ.get("AGENT_PORT", cls.model_fields["agent_port"].default)),
            agent_url=os.environ.get("AGENT_URL", ""),
            data_dir=os.environ.get("DATA_DIR", cls.model_fields["data_dir"].default),
            credentials_path=os.environ.get("CREDENTIALS_PATH", cls.model_fields["credentials_path"].default),
            session_secret=os.environ.get("SESSION_SECRET") or _s.token_hex(32),
            channel_agent_url=os.environ.get("CHANNEL_AGENT_URL", cls.model_fields["channel_agent_url"].default),
            session_title_delay_sec=int(_get("SESSION_TITLE_DELAY_SEC", cls.model_fields["session_title_delay_sec"].default)),
            archive_refresh_interval_sec=int(_get("ARCHIVE_REFRESH_INTERVAL_SEC", cls.model_fields["archive_refresh_interval_sec"].default)),
            agents_refresh_interval_sec=int(_get("AGENTS_REFRESH_INTERVAL_SEC", cls.model_fields["agents_refresh_interval_sec"].default)),
        )
