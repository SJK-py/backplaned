"""bp_router.llm.service — LlmService and provider-neutral types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Literal, Optional, Union

if TYPE_CHECKING:
    from bp_router.app import AppState
    from bp_router.llm.providers.base import ProviderAdapter
    from bp_router.settings import Settings


# ---------------------------------------------------------------------------
# Provider-neutral types
# ---------------------------------------------------------------------------


@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: Union[str, list[dict[str, Any]]]
    name: Optional[str] = None
    tool_call_id: Optional[str] = None


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    """JSON Schema for the tool's arguments."""


ToolChoice = Union[Literal["auto", "none", "required"], dict[str, Any]]


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_microusd: int = 0


@dataclass
class LlmResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: Literal["stop", "length", "tool_calls", "content_filter", "error"] = "stop"
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw: dict[str, Any] = field(default_factory=dict)
    """Provider-specific extras (citations, grounding metadata, etc.)."""


@dataclass
class LlmDelta:
    """One incremental event in a streaming response."""

    text: Optional[str] = None
    tool_call: Optional[ToolCall] = None
    finish_reason: Optional[str] = None
    usage: Optional[TokenUsage] = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class LlmService:
    """Router-side LLM facade.

    Configuration: deployment-config aliases (`default`, `fast`,
    `gemini-2.5`, ...) map to (provider, concrete_model, credentials).
    The mapping table lives in `acl.yaml`-style config (TODO: define
    `llm.yaml` location).

    On each call:
      1. Resolve alias → provider adapter.
      2. Pull credentials from secrets backend (cached).
      3. Apply per-user quota (LLM input/output token caps, USD cost cap).
      4. Delegate to provider adapter.
      5. Record usage (audit + metrics + quota counters).
    """

    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        self._adapters: dict[str, "ProviderAdapter"] = {}

    # ------------------------------------------------------------------
    # Public API (mirrors sdk.LlmService surface)
    # ------------------------------------------------------------------

    async def generate(
        self,
        messages: list[Message],
        *,
        model: str = "default",
        tools: Optional[list[ToolSpec]] = None,
        tool_choice: Optional[ToolChoice] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        provider_options: Optional[dict[str, Any]] = None,
        user_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Union[LlmResponse, AsyncIterator[LlmDelta]]:
        raise NotImplementedError

    async def embed(
        self,
        text: Union[str, list[str]],
        *,
        model: str = "default",
        user_id: Optional[str] = None,
    ) -> list[list[float]]:
        raise NotImplementedError

    async def count_tokens(
        self,
        messages: list[Message],
        *,
        model: str = "default",
    ) -> int:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Provider resolution
    # ------------------------------------------------------------------

    def _resolve(self, model_alias: str) -> "ProviderAdapter":
        """Return the provider adapter for an alias. Cached after first use."""
        raise NotImplementedError
