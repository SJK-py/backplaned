"""bp_router.llm.service — LlmService and provider-neutral types."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Literal, Optional, Union

if TYPE_CHECKING:
    from bp_router.llm.providers.base import ProviderAdapter
    from bp_router.settings import Settings

logger = logging.getLogger(__name__)


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


@dataclass
class LlmDelta:
    text: Optional[str] = None
    tool_call: Optional[ToolCall] = None
    finish_reason: Optional[str] = None
    usage: Optional[TokenUsage] = None


# ---------------------------------------------------------------------------
# Model alias resolution
# ---------------------------------------------------------------------------


@dataclass
class _ModelBinding:
    provider: str
    concrete_model: str
    api_key_ref: str  # secret_ref string


def _default_alias_map() -> dict[str, _ModelBinding]:
    """Built-in alias bindings. Deployments override via ROUTER_LLM_ALIAS_*
    env vars or a future llm.yaml.

    Default reads provider keys from environment variables — this is the
    simplest path for the skeleton; production deployments should use
    secret_ref.
    """
    return {
        "default": _ModelBinding("gemini", "gemini-2.5-flash", "env://GEMINI_API_KEY"),
        "gemini-2.5": _ModelBinding("gemini", "gemini-2.5-pro", "env://GEMINI_API_KEY"),
        "gemini-2.5-flash": _ModelBinding(
            "gemini", "gemini-2.5-flash", "env://GEMINI_API_KEY"
        ),
        "gemini-2.5-pro": _ModelBinding(
            "gemini", "gemini-2.5-pro", "env://GEMINI_API_KEY"
        ),
    }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class LlmService:
    """Router-side LLM facade.

    Holds:
      - alias → (provider, concrete_model, api_key_ref) bindings
      - per-binding ProviderAdapter instances (lazy)

    Hot path:
      1. Resolve alias.
      2. Lazy-construct + cache adapter (decoded API key).
      3. Delegate. Record metrics + token counts on the way back.
    """

    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        self._aliases: dict[str, _ModelBinding] = _default_alias_map()
        self._adapters: dict[str, "ProviderAdapter"] = {}

    def register_alias(
        self,
        alias: str,
        *,
        provider: str,
        concrete_model: str,
        api_key_ref: str,
    ) -> None:
        self._aliases[alias] = _ModelBinding(
            provider=provider,
            concrete_model=concrete_model,
            api_key_ref=api_key_ref,
        )
        # Invalidate any cached adapter for this concrete_model.
        self._adapters.pop(self._adapter_key(self._aliases[alias]), None)

    def _adapter_key(self, binding: _ModelBinding) -> str:
        return f"{binding.provider}::{binding.concrete_model}"

    # ------------------------------------------------------------------
    # Public API
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
        adapter = self._resolve(model)
        result = await adapter.generate(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            provider_options=provider_options,
        )

        # Record metrics. For the streaming case, we'd ideally instrument
        # the iterator — left as a TODO; for now record on completion only.
        if not stream:
            self._record(adapter, model, result, user_id=user_id, task_id=task_id)
        return result

    async def embed(
        self,
        text: Union[str, list[str]],
        *,
        model: str = "default",
        user_id: Optional[str] = None,
    ) -> list[list[float]]:
        adapter = self._resolve(model)
        return await adapter.embed(text)

    async def count_tokens(
        self,
        messages: list[Message],
        *,
        model: str = "default",
    ) -> int:
        adapter = self._resolve(model)
        return await adapter.count_tokens(messages)

    # ------------------------------------------------------------------
    # Provider resolution
    # ------------------------------------------------------------------

    def _resolve(self, model_alias: str) -> "ProviderAdapter":
        binding = self._aliases.get(model_alias)
        if binding is None:
            raise KeyError(f"unknown LLM model alias: {model_alias!r}")

        cache_key = self._adapter_key(binding)
        adapter = self._adapters.get(cache_key)
        if adapter is None:
            adapter = self._build_adapter(binding)
            self._adapters[cache_key] = adapter
        return adapter

    def _build_adapter(self, binding: _ModelBinding) -> "ProviderAdapter":
        from bp_router.security.secrets import resolve_secret_ref  # noqa: PLC0415

        api_key = resolve_secret_ref(binding.api_key_ref)

        if binding.provider == "gemini":
            from bp_router.llm.providers.gemini import GeminiAdapter  # noqa: PLC0415

            return GeminiAdapter(
                concrete_model=binding.concrete_model, api_key=api_key
            )
        raise NotImplementedError(
            f"provider {binding.provider!r} adapter not yet wired"
        )

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def _record(
        self,
        adapter: "ProviderAdapter",
        alias: str,
        result: Any,
        *,
        user_id: Optional[str],
        task_id: Optional[str],
    ) -> None:
        if not isinstance(result, LlmResponse):
            return
        try:
            from bp_router.observability.metrics import (  # noqa: PLC0415
                llm_calls_total,
                llm_cost_microusd_total,
                llm_tokens_total,
            )

            llm_calls_total.labels(
                model=alias, provider=adapter.provider_name, status=result.finish_reason
            ).inc()
            llm_tokens_total.labels(model=alias, direction="in").inc(
                result.usage.input_tokens
            )
            llm_tokens_total.labels(model=alias, direction="out").inc(
                result.usage.output_tokens
            )
            if result.usage.cost_microusd:
                llm_cost_microusd_total.labels(model=alias).inc(
                    result.usage.cost_microusd
                )
        except Exception:  # noqa: BLE001
            logger.debug("llm metric record failed", exc_info=True)
