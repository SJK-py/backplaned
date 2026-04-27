"""bp_router.llm.providers.gemini — Gemini provider adapter (skeleton).

Wraps `google-genai` (deferred import). Translates neutral `Message`
to Gemini `Content` parts; honours `provider_options` for native
features (grounding, code execution, image/video generation,
thinking budgets).
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional, Union

from bp_router.llm.providers.base import ProviderAdapter
from bp_router.llm.service import (
    LlmDelta,
    LlmResponse,
    Message,
    ToolChoice,
    ToolSpec,
)


class GeminiAdapter(ProviderAdapter):
    provider_name = "gemini"

    def __init__(self, *, concrete_model: str, api_key: str) -> None:
        self.concrete_model = concrete_model
        self._api_key = api_key
        # Defer the genai client construction to first use to avoid
        # importing the SDK if no Gemini calls are made.
        self._client: Optional[Any] = None

    async def generate(
        self,
        messages: list[Message],
        *,
        tools: Optional[list[ToolSpec]] = None,
        tool_choice: Optional[ToolChoice] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        provider_options: Optional[dict[str, Any]] = None,
    ) -> Union[LlmResponse, AsyncIterator[LlmDelta]]:
        # Implementation: lazy-import google.genai, build a Content[] from
        # messages, attach tools (neutral → genai.types.Tool), forward
        # provider_options as-is into GenerateContentConfig (system_instruction,
        # tools, thinking_config, safety_settings, ...).
        raise NotImplementedError

    async def embed(self, text: Union[str, list[str]]) -> list[list[float]]:
        raise NotImplementedError

    async def count_tokens(self, messages: list[Message]) -> int:
        raise NotImplementedError
