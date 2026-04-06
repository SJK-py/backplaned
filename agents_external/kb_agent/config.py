"""
kb_agent/config.py — Configuration for the Knowledge Base Agent.
"""

from __future__ import annotations

import os
from pydantic import BaseModel


class AgentConfig(BaseModel):
    router_url: str = "http://localhost:8000"
    invitation_token: str = ""
    receive_url: str = "http://localhost:8086/receive"

    agent_host: str = "0.0.0.0"
    agent_port: int = 8086
    agent_id: str = "kb_agent"
    agent_auth_token: str = ""
    agent_endpoint_url: str = ""
    admin_password: str = ""
    session_secret: str = ""

    # Embedding
    embed_base_url: str = "http://172.23.90.91:8000/api/v1"
    embed_api_key: str = "placeholder"
    embed_model: str = "Qwen3-Embedding-4B-GGUF"
    embed_timeout: float = 30.0
    vector_dim: int = 2560

    # LLM
    llm_agent_id: str = "llm_agent"
    default_model_id: str = ""

    # Chunking
    chunk_len_max: int = 2000
    chunk_len_min: int = 1000
    chunk_overlap: int = 100

    # Agents
    md_converter_id: str = "md_converter"

    # Paths
    data_dir: str = "data"
    tool_timeout: int = 120

    @classmethod
    def from_env(cls) -> AgentConfig:
        import secrets as _s
        if not os.environ.get("ADMIN_PASSWORD"):
            import warnings as _w
            _w.warn("ADMIN_PASSWORD is not set — web UI login will be unavailable until configured", stacklevel=2)
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
            embed_base_url=os.environ.get("EMBED_BASE_URL", cls.model_fields["embed_base_url"].default),
            embed_api_key=os.environ.get("EMBED_API_KEY", cls.model_fields["embed_api_key"].default),
            embed_model=os.environ.get("EMBED_MODEL", cls.model_fields["embed_model"].default),
            embed_timeout=float(os.environ.get("EMBED_TIMEOUT", cls.model_fields["embed_timeout"].default)),
            vector_dim=int(os.environ.get("VECTOR_DIM", cls.model_fields["vector_dim"].default)),
            llm_agent_id=os.environ.get("LLM_AGENT_ID", cls.model_fields["llm_agent_id"].default),
            default_model_id=os.environ.get("DEFAULT_MODEL_ID", ""),
            chunk_len_max=int(os.environ.get("CHUNK_LEN_MAX", cls.model_fields["chunk_len_max"].default)),
            chunk_len_min=int(os.environ.get("CHUNK_LEN_MIN", cls.model_fields["chunk_len_min"].default)),
            chunk_overlap=int(os.environ.get("CHUNK_OVERLAP", cls.model_fields["chunk_overlap"].default)),
            md_converter_id=os.environ.get("MD_CONVERTER_ID", cls.model_fields["md_converter_id"].default),
            data_dir=os.environ.get("DATA_DIR", cls.model_fields["data_dir"].default),
            tool_timeout=int(os.environ.get("TOOL_TIMEOUT", cls.model_fields["tool_timeout"].default)),
            agent_auth_token=os.environ.get("AGENT_AUTH_TOKEN", cls.model_fields["agent_auth_token"].default),
        )
