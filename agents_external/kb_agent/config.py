"""
kb_agent/config.py — Configuration for the Knowledge Base Agent.

Infrastructure settings (secrets, URLs, ports) come from environment
variables / .env.  Runtime settings (model IDs, embedding config,
chunking, timeouts) come from data/config.json.
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
    endpoint_url: str = "http://localhost:8086/receive"
    agent_host: str = "0.0.0.0"
    agent_port: int = 8086
    agent_id: str = "kb_agent"
    agent_auth_token: str = ""
    agent_url: str = ""
    admin_password: str = ""
    session_secret: str = ""
    data_dir: str = "data"
    embed_api_key: str = "placeholder"

    # --- Runtime (from data/config.json) ---
    embed_base_url: str = "http://172.23.90.91:8000/api/v1"
    embed_model: str = "Qwen3-Embedding-4B-GGUF"
    embed_timeout: float = 30.0
    vector_dim: int = 2560
    llm_agent_id: str = "llm_agent"
    default_model_id: str = ""
    chunk_len_max: int = 2000
    chunk_len_min: int = 1000
    chunk_overlap: int = 100
    md_converter_id: str = "md_converter"
    tool_timeout: int = 120

    @classmethod
    def from_env(cls) -> AgentConfig:
        import secrets as _s
        if not os.environ.get("ADMIN_PASSWORD"):
            import warnings as _w
            _w.warn("ADMIN_PASSWORD is not set — web UI login will be unavailable until configured", stacklevel=2)
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
            invitation_token=os.environ.get("INVITATION_TOKEN", ""),
            endpoint_url=os.environ.get("ENDPOINT_URL", cls.model_fields["endpoint_url"].default),
            agent_host=os.environ.get("AGENT_HOST", cls.model_fields["agent_host"].default),
            agent_port=int(os.environ.get("AGENT_PORT", cls.model_fields["agent_port"].default)),
            agent_id=os.environ.get("AGENT_ID", cls.model_fields["agent_id"].default),
            agent_url=os.environ.get("AGENT_URL", cls.model_fields["agent_url"].default),
            admin_password=os.environ.get("ADMIN_PASSWORD", cls.model_fields["admin_password"].default),
            session_secret=os.environ.get("SESSION_SECRET") or _s.token_hex(32),
            agent_auth_token=os.environ.get("AGENT_AUTH_TOKEN", cls.model_fields["agent_auth_token"].default),
            data_dir=os.environ.get("DATA_DIR", cls.model_fields["data_dir"].default),
            embed_api_key=os.environ.get("EMBED_API_KEY", cls.model_fields["embed_api_key"].default),
            # Runtime (config.json > env fallback > default)
            embed_base_url=_get("EMBED_BASE_URL", default=cls.model_fields["embed_base_url"].default),
            embed_model=_get("EMBED_MODEL", default=cls.model_fields["embed_model"].default),
            embed_timeout=float(_get("EMBED_TIMEOUT", default=cls.model_fields["embed_timeout"].default)),
            vector_dim=int(_get("VECTOR_DIM", default=cls.model_fields["vector_dim"].default)),
            llm_agent_id=_get("LLM_AGENT_ID", default=cls.model_fields["llm_agent_id"].default),
            default_model_id=_get("DEFAULT_MODEL_ID", default=""),
            chunk_len_max=int(_get("CHUNK_LEN_MAX", default=cls.model_fields["chunk_len_max"].default)),
            chunk_len_min=int(_get("CHUNK_LEN_MIN", default=cls.model_fields["chunk_len_min"].default)),
            chunk_overlap=int(_get("CHUNK_OVERLAP", default=cls.model_fields["chunk_overlap"].default)),
            md_converter_id=_get("MD_CONVERTER_ID", default=cls.model_fields["md_converter_id"].default),
            tool_timeout=int(_get("TOOL_TIMEOUT", default=cls.model_fields["tool_timeout"].default)),
        )
