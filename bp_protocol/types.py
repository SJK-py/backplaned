"""bp_protocol.types — Common Pydantic models shared by router and SDK.

These models are protocol-stable: changes here are wire-breaking.
See `docs/sdk/core.md` and `docs/sdk/services.md` for the
agent-side API surface that consumes them.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskState(str, Enum):
    """Task state machine states. See `docs/router/state.md` §1."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_CHILDREN = "WAITING_CHILDREN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"

    @property
    def is_terminal(self) -> bool:
        return self in {
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.TIMED_OUT,
        }


class TaskStatus(str, Enum):
    """Terminal task outcome reported on Result frames."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class TaskPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


ProxyFileProtocol = Literal["router-proxy", "presigned", "localfile", "http"]


# ---------------------------------------------------------------------------
# ProxyFile
# ---------------------------------------------------------------------------


class ProxyFile(BaseModel):
    """Reference to a file managed by the router's pluggable storage backend.

    Agents pass `ProxyFile` objects to share files; the router may rewrite
    `protocol` and `path` between agents (e.g. converting a `localfile`
    produced by an embedded agent into a `presigned` URL for an external
    consumer).

    See `docs/router/storage.md` §2 for storage semantics.
    """

    path: str
    """Backend-resolvable path or URL."""

    protocol: ProxyFileProtocol
    """Transport protocol for this file reference."""

    key: Optional[str] = None
    """Optional access key issued by the router for one-shot fetch URLs."""

    sha256: Optional[str] = None
    """Content hash; required for content-addressed dedup on upload."""

    byte_size: Optional[int] = None
    """File size in bytes, when known."""

    mime_type: Optional[str] = None

    original_filename: Optional[str] = None
    """Preserved filename hint when path is opaque."""


# ---------------------------------------------------------------------------
# Agent metadata
# ---------------------------------------------------------------------------


class AgentInfo(BaseModel):
    """Metadata an agent publishes to the router on registration.

    Capabilities and tags drive ACL evaluation (`docs/acl.md`).
    `accepts_schema` and `produces_schema` are JSON Schema fragments used
    by the router to validate `NewTask.payload` and `Result.output` at the
    boundary, before any handler runs.
    """

    agent_id: str
    """Stable globally-unique identifier."""

    description: str
    """Human-readable, used in catalog entries."""

    capabilities: list[str] = Field(default_factory=list)
    """Capabilities this agent provides. See `docs/acl.md` §3."""

    requires_capabilities: list[str] = Field(default_factory=list)
    """Capabilities this agent's handlers may invoke on others."""

    tags: list[str] = Field(default_factory=list)
    """Free-form labels (`tier:1`, `team:coding`, `provider:gemini`, ...)."""

    accepts_schema: Optional[dict[str, Any]] = None
    """JSON Schema for `NewTask.payload`. Validated at admit time."""

    produces_schema: Optional[dict[str, Any]] = None
    """JSON Schema for `Result.output`."""

    documentation_url: Optional[str] = None
    """Optional URL to fetch the agent's full markdown docs."""

    hidden: bool = False
    """Suppress from automatic LLM tool generation."""

    min_role: Optional[str] = None
    """Minimum user role required to invoke. None = no restriction."""

    min_tier: Optional[str] = None
    """Minimum user tier required to invoke. None = no restriction."""


# ---------------------------------------------------------------------------
# LLM payloads
# ---------------------------------------------------------------------------


class LLMData(BaseModel):
    """High-level LLM prompt container forwarded to LLM-backed agents."""

    prompt: str
    agent_instruction: Optional[str] = None
    context: Optional[str] = None


class LLMCall(BaseModel):
    """Low-level LLM inference request — full messages array.

    Used when an agent needs complete control over the conversation fed to
    the model (multi-turn tool calls, retrieval-augmented generation, etc.).
    """

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: Optional[Any] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    model_id: Optional[str] = None
    """Deployment-config alias (`default`, `fast`, `gemini-2.5`, ...)."""
    provider_options: Optional[dict[str, Any]] = None
    """Opaque blob forwarded to the provider client; used for native features."""


# ---------------------------------------------------------------------------
# Standard agent output
# ---------------------------------------------------------------------------


class AgentOutput(BaseModel):
    """Standardised return value produced by handlers.

    Either `content` or `files` (or both) should be populated.
    """

    content: Optional[str] = None
    files: list[ProxyFile] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    """Free-form additional output (token usage, citations, etc.)."""
