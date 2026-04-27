"""bp_router.llm.providers.gemini — Gemini provider adapter.

Wraps `google-genai` (deferred import). Translates neutral `Message`
to Gemini `Content` parts; honours `provider_options` for native
features (grounding, code execution, image/video generation,
thinking budgets).
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Optional, Union

from bp_router.llm.providers.base import ProviderAdapter
from bp_router.llm.service import (
    LlmDelta,
    LlmResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolChoice,
    ToolSpec,
)

logger = logging.getLogger(__name__)


class GeminiAdapter(ProviderAdapter):
    provider_name = "gemini"

    def __init__(self, *, concrete_model: str, api_key: str) -> None:
        self.concrete_model = concrete_model
        self._api_key = api_key
        self._client: Optional[Any] = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from google import genai  # noqa: PLC0415
            except ImportError as exc:
                raise RuntimeError(
                    "google-genai not installed; `pip install google-genai`"
                ) from exc
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    # ------------------------------------------------------------------
    # generate
    # ------------------------------------------------------------------

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
        client = self._get_client()

        contents, system_instruction = self._convert_messages(messages)
        config = self._build_config(
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            provider_options=provider_options,
            system_instruction=system_instruction,
        )

        if stream:
            return self._generate_stream(client, contents, config)

        # Non-streaming.
        resp = await client.aio.models.generate_content(
            model=self.concrete_model,
            contents=contents,
            config=config,
        )
        return self._convert_response(resp)

    async def _generate_stream(
        self, client: Any, contents: Any, config: Any
    ) -> AsyncIterator[LlmDelta]:
        async for chunk in client.aio.models.generate_content_stream(
            model=self.concrete_model,
            contents=contents,
            config=config,
        ):
            text = getattr(chunk, "text", None)
            tool_call = self._extract_tool_call(chunk)
            usage = self._extract_usage(chunk)
            finish = self._extract_finish(chunk)
            if text or tool_call or usage or finish:
                yield LlmDelta(
                    text=text,
                    tool_call=tool_call,
                    finish_reason=finish,
                    usage=usage,
                )

    # ------------------------------------------------------------------
    # embed / count_tokens
    # ------------------------------------------------------------------

    async def embed(self, text: Union[str, list[str]]) -> list[list[float]]:
        client = self._get_client()
        if isinstance(text, str):
            text = [text]
        result = await client.aio.models.embed_content(
            model=self.concrete_model,
            contents=text,
        )
        # google-genai returns Embeddings — extract the .values lists.
        return [list(e.values) for e in result.embeddings]

    async def count_tokens(self, messages: list[Message]) -> int:
        client = self._get_client()
        contents, _ = self._convert_messages(messages)
        result = await client.aio.models.count_tokens(
            model=self.concrete_model, contents=contents
        )
        return int(getattr(result, "total_tokens", 0))

    # ------------------------------------------------------------------
    # Translation helpers
    # ------------------------------------------------------------------

    def _convert_messages(
        self, messages: list[Message]
    ) -> tuple[list[dict[str, Any]], Optional[str]]:
        """Turn neutral Messages into Gemini contents + system_instruction."""
        contents: list[dict[str, Any]] = []
        system_instruction: Optional[str] = None
        for m in messages:
            if m.role == "system":
                # Gemini accepts a single system instruction string.
                if isinstance(m.content, str):
                    system_instruction = (
                        f"{system_instruction}\n{m.content}"
                        if system_instruction
                        else m.content
                    )
                continue
            if m.role == "tool":
                contents.append(
                    {
                        "role": "tool",
                        "parts": [
                            {
                                "function_response": {
                                    "name": m.name or "",
                                    "response": (
                                        {"result": m.content}
                                        if isinstance(m.content, str)
                                        else m.content
                                    ),
                                }
                            }
                        ],
                    }
                )
                continue
            role = "user" if m.role == "user" else "model"
            if isinstance(m.content, str):
                parts: list[Any] = [{"text": m.content}]
            else:
                parts = m.content
            contents.append({"role": role, "parts": parts})
        return contents, system_instruction

    def _build_config(
        self,
        *,
        tools: Optional[list[ToolSpec]],
        tool_choice: Optional[ToolChoice],
        temperature: Optional[float],
        max_tokens: Optional[int],
        provider_options: Optional[dict[str, Any]],
        system_instruction: Optional[str],
    ) -> Any:
        from google.genai import types as gtypes  # noqa: PLC0415

        cfg_kwargs: dict[str, Any] = {}
        if temperature is not None:
            cfg_kwargs["temperature"] = temperature
        if max_tokens is not None:
            cfg_kwargs["max_output_tokens"] = max_tokens
        if system_instruction:
            cfg_kwargs["system_instruction"] = system_instruction

        # Tool list: combine neutral ToolSpec functions + provider-specific
        # blocks from provider_options["tools"] (e.g. {"google_search": {}}).
        tool_blocks: list[Any] = []
        if tools:
            from bp_sdk.tools import _gemini_strip_schema  # noqa: PLC0415

            tool_blocks.append(
                {
                    "function_declarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": _gemini_strip_schema(t.parameters),
                        }
                        for t in tools
                    ]
                }
            )
        if provider_options:
            extra = provider_options.get("tools") or []
            tool_blocks.extend(extra)
            for k in (
                "thinking_budget_tokens",
                "safety_settings",
                "response_mime_type",
                "response_schema",
                "stop_sequences",
            ):
                if k in provider_options:
                    cfg_kwargs[k] = provider_options[k]

        if tool_blocks:
            cfg_kwargs["tools"] = tool_blocks

        # tool_choice mapping (best-effort).
        if tool_choice == "required":
            cfg_kwargs["tool_config"] = {"function_calling_config": {"mode": "ANY"}}
        elif tool_choice == "none":
            cfg_kwargs["tool_config"] = {"function_calling_config": {"mode": "NONE"}}
        elif tool_choice == "auto":
            cfg_kwargs["tool_config"] = {"function_calling_config": {"mode": "AUTO"}}
        elif isinstance(tool_choice, dict):
            cfg_kwargs["tool_config"] = tool_choice

        try:
            return gtypes.GenerateContentConfig(**cfg_kwargs)
        except Exception:
            # Fall back to a plain dict if the types module shape changes.
            return cfg_kwargs

    def _convert_response(self, resp: Any) -> LlmResponse:
        text = getattr(resp, "text", "") or ""
        tool_calls = []

        for cand in getattr(resp, "candidates", None) or []:
            content = getattr(cand, "content", None)
            for part in getattr(content, "parts", None) or []:
                fc = getattr(part, "function_call", None)
                if fc is not None:
                    tool_calls.append(
                        ToolCall(
                            id=getattr(fc, "id", "") or fc.name,
                            name=fc.name,
                            args=dict(getattr(fc, "args", {}) or {}),
                        )
                    )

        usage = self._extract_usage(resp) or TokenUsage()
        finish = self._extract_finish(resp) or "stop"

        return LlmResponse(
            text=text,
            tool_calls=tool_calls,
            finish_reason=finish if finish in {
                "stop", "length", "tool_calls", "content_filter", "error"
            } else "stop",
            usage=usage,
            raw=getattr(resp, "model_dump", lambda: {})() or {},
        )

    def _extract_usage(self, resp: Any) -> Optional[TokenUsage]:
        meta = getattr(resp, "usage_metadata", None)
        if meta is None:
            return None
        return TokenUsage(
            input_tokens=int(getattr(meta, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(meta, "candidates_token_count", 0) or 0),
        )

    def _extract_finish(self, resp: Any) -> Optional[str]:
        cand = (getattr(resp, "candidates", None) or [None])[0]
        fr = getattr(cand, "finish_reason", None) if cand else None
        if fr is None:
            return None
        # Gemini returns enum-ish values; coerce to a known one.
        s = str(fr).lower()
        if "stop" in s:
            return "stop"
        if "max_tokens" in s or "length" in s:
            return "length"
        if "function" in s or "tool" in s:
            return "tool_calls"
        if "safety" in s or "blocked" in s:
            return "content_filter"
        return "stop"

    def _extract_tool_call(self, chunk: Any) -> Optional[ToolCall]:
        for cand in getattr(chunk, "candidates", None) or []:
            content = getattr(cand, "content", None)
            for part in getattr(content, "parts", None) or []:
                fc = getattr(part, "function_call", None)
                if fc is not None:
                    return ToolCall(
                        id=getattr(fc, "id", "") or fc.name,
                        name=fc.name,
                        args=dict(getattr(fc, "args", {}) or {}),
                    )
        return None
