"""
agents/llm_agent/agent.py — Centralized LLM inference agent.

Pure inference service: accepts either LLMCall (raw messages + tools) or
LLMData (high-level prompt) payloads, calls the configured LLM backend,
and returns a normalized response.  Does NOT dispatch tool calls — callers
handle tool execution themselves.

Supports multiple model configurations via config.json, with automatic
retry and fallback chains.

Providers: openai_compat, openai, anthropic, gemini.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from helper import (
    AgentInfo,
    AgentOutput,
    LLMCall,
    LLMData,
    ProxyFile,
    ProxyFileManager,
    build_result_request,
)

logger = logging.getLogger("llm_agent")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_AGENT_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _AGENT_DIR / "config.json"
_OUR_AGENT_ID = "llm_agent"


def _load_config() -> dict[str, Any]:
    """Read config.json from disk on every call (no caching needed)."""
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Failed to read config.json: %s", exc)
        return {}


ROUTER_URL: str = os.environ.get("ROUTER_URL", "http://localhost:8000")


def _get_model_config(cfg: dict[str, Any], model_id: Optional[str]) -> dict[str, Any]:
    """Resolve a model_id to its config dict, falling back to 'default'."""
    models = cfg.get("models", {})
    mid = model_id or "default"
    model_cfg = models.get(mid)
    if model_cfg is None and mid != "default":
        logger.warning("Model '%s' not found, falling back to 'default'", mid)
        model_cfg = models.get("default")
    if model_cfg is None:
        raise ValueError(f"No model config found for '{mid}' and no 'default' defined")
    return model_cfg


# ---------------------------------------------------------------------------
# Per-user model ACL
# ---------------------------------------------------------------------------

def _is_model_allowed(cfg: dict[str, Any], model_id: str, user_id: str) -> bool:
    """Check whether *user_id* may use *model_id*.

    A model is allowed if ANY of:
    1. model_id is ``"default"``
    2. The model entry has ``"available_to_all": true``
    3. The user appears in ``allowed_models`` with that model_id listed
    """
    if model_id == "default":
        return True
    models = cfg.get("models", {})
    model_cfg = models.get(model_id)
    if model_cfg and model_cfg.get("available_to_all"):
        return True
    allowed: dict[str, str] = cfg.get("allowed_models", {})
    user_models_str = allowed.get(user_id, "")
    user_models = {m.strip() for m in user_models_str.split(",") if m.strip()}
    return model_id in user_models


def _get_allowed_model_ids(cfg: dict[str, Any], user_id: str) -> list[str]:
    """Return the list of model_ids available to *user_id*."""
    models = cfg.get("models", {})
    allowed: list[str] = []
    for mid, mcfg in models.items():
        if _is_model_allowed(cfg, mid, user_id):
            allowed.append(mid)
    return sorted(allowed)


# ---------------------------------------------------------------------------
# AgentInfo
# ---------------------------------------------------------------------------

AGENT_INFO = AgentInfo(
    agent_id=_OUR_AGENT_ID,
    description=(
        "Centralized LLM inference agent. Accepts LLMCall (raw messages + tools) "
        "or LLMData (high-level prompt) and returns a normalized response with "
        "content and tool_calls. Supports multiple model configs via model_id."
    ),
    input_schema="llmcall: Optional[LLMCall], llmdata: Optional[LLMData], model_id: Optional[str], user_id: Optional[str], files: Optional[List[ProxyFile]]",
    output_schema="content: str (JSON: {content, tool_calls})",
    required_input=[],
    hidden=True,
)

# ---------------------------------------------------------------------------
# Thinking extraction helpers
# ---------------------------------------------------------------------------

import re as _re

_THINK_TAG_RE = _re.compile(r"<think>(.*?)</think>", _re.DOTALL)


def _strip_thinking(text: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """
    Extract and strip <think>...</think> blocks from content.

    Returns (clean_content, thinking_text).  If no thinking tags are found,
    returns (text, None).
    """
    if not text:
        return text, None
    matches = _THINK_TAG_RE.findall(text)
    if not matches:
        return text, None
    thinking = "\n".join(m.strip() for m in matches)
    clean = _THINK_TAG_RE.sub("", text).strip()
    return clean or None, thinking


def _normalize_response(
    content: Optional[str],
    tool_calls: list[dict[str, Any]],
    reasoning_content: Optional[str],
    usage: Optional[dict[str, int]] = None,
    thinking_blocks: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """
    Build normalized response dict.

    Strips ``<think>...</think>`` tags from content (used by Qwen3,
    DeepSeek-R1, etc.) and captures them as thinking_blocks alongside
    any provider-native thinking data.

    ``thinking_blocks`` carries opaque provider-specific thinking data
    that callers must round-trip in multi-turn tool-calling conversations.
    Callers can also extract a summary for verbose progress events.
    """
    # Extract thinking from <think> tags in content
    clean_content, tag_thinking = _strip_thinking(content)

    # Merge all thinking sources into thinking_blocks
    all_blocks = list(thinking_blocks or [])
    # Add thinking from <think> tags and reasoning_content as generic blocks
    extra_thinking = "\n".join(t for t in [reasoning_content, tag_thinking] if t) or None
    if extra_thinking and not all_blocks:
        # Only add as a block if no provider-native blocks already captured it
        all_blocks.append({"type": "thinking", "text": extra_thinking})

    result: dict[str, Any] = {"content": clean_content, "tool_calls": tool_calls}
    if usage:
        result["usage"] = usage
    if all_blocks:
        result["thinking_blocks"] = all_blocks
    return result


# ---------------------------------------------------------------------------
# Client caching — reuse connections across LLM calls
# ---------------------------------------------------------------------------

_openai_clients: dict[tuple, Any] = {}   # (provider, base_url, api_key) → AsyncOpenAI
_anthropic_clients: dict[tuple, Any] = {}  # (api_key,) → AsyncAnthropic
_gemini_clients: dict[tuple, Any] = {}     # (api_key,) → genai.Client


def _get_openai_client(provider: str, base_url: Optional[str], api_key: str, timeout: float) -> Any:
    """Return a cached AsyncOpenAI client, creating one if needed."""
    from openai import AsyncOpenAI
    key = (provider, base_url, api_key, timeout)
    client = _openai_clients.get(key)
    if client is None:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        _openai_clients[key] = client
    return client


def _get_anthropic_client(api_key: str, timeout: float) -> Any:
    """Return a cached AsyncAnthropic client, creating one if needed."""
    import anthropic
    key = (api_key, timeout)
    client = _anthropic_clients.get(key)
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout)
        _anthropic_clients[key] = client
    return client


def _get_gemini_client(api_key: str) -> Any:
    """Return a cached google-genai Client, creating one if needed."""
    from google import genai
    key = (api_key,)
    client = _gemini_clients.get(key)
    if client is None:
        client = genai.Client(api_key=api_key)
        _gemini_clients[key] = client
    return client


# ---------------------------------------------------------------------------
# Provider-specific LLM call implementations
# ---------------------------------------------------------------------------


async def _call_openai_compat(
    model_cfg: dict[str, Any],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    temperature: Optional[float],
    max_tokens: Optional[int],
    tool_choice: Optional[Any] = None,
) -> dict[str, Any]:
    """Call an OpenAI-compatible endpoint (also used for native openai)."""
    provider = model_cfg["provider"]
    base_url = model_cfg.get("base_url")
    if provider == "openai":
        base_url = None  # Use OpenAI's default endpoint
    elif provider == "gemini" and not base_url:
        base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"

    client = _get_openai_client(
        provider, base_url,
        model_cfg.get("api_key", ""),
        float(model_cfg.get("timeout", 60)),
    )

    kwargs: dict[str, Any] = {
        "model": model_cfg["model"],
        "messages": messages,
        "temperature": temperature if temperature is not None else model_cfg.get("temperature", 0.7),
        "max_tokens": max_tokens if max_tokens is not None else model_cfg.get("max_tokens", 4096),
    }
    if tools:
        kwargs["tools"] = tools
        if tool_choice is not None:
            # openai_compat backends (e.g. llama.cpp) only accept string
            # values for tool_choice.  Convert the object form to "required".
            if provider == "openai_compat" and isinstance(tool_choice, dict):
                kwargs["tool_choice"] = "required"
            else:
                kwargs["tool_choice"] = tool_choice
        else:
            kwargs["tool_choice"] = "auto"

    # reasoning_effort — OpenAI o-series / Gemini OpenAI-compat
    reasoning_effort = model_cfg.get("reasoning_effort")
    if reasoning_effort and provider in ("openai", "gemini"):
        kwargs["reasoning_effort"] = reasoning_effort

    resp = await client.chat.completions.create(**kwargs)

    choice = resp.choices[0]
    tool_calls: list[dict[str, Any]] = []
    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except Exception:
                args = {}
            tool_calls.append({
                "id": tc.id,
                "name": tc.function.name,
                "arguments": args,
            })

    # Extract reasoning_content if the provider returns it (e.g. o1)
    reasoning = getattr(choice.message, "reasoning_content", None)

    usage = None
    if resp.usage:
        usage = {
            "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(resp.usage, "completion_tokens", 0) or 0,
        }

    return _normalize_response(choice.message.content, tool_calls, reasoning, usage)


async def _call_anthropic(
    model_cfg: dict[str, Any],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    temperature: Optional[float],
    max_tokens: Optional[int],
    tool_choice: Optional[Any] = None,
) -> dict[str, Any]:
    """Call the Anthropic Messages API."""
    client = _get_anthropic_client(
        model_cfg.get("api_key", ""),
        float(model_cfg.get("timeout", 60)),
    )

    # Convert OpenAI-format tools to Anthropic format.
    ant_tools = []
    for t in tools:
        fn = t.get("function", t)
        ant_tools.append({
            "name": fn.get("name", t.get("name", "")),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", fn.get("input_schema", {"type": "object", "properties": {}})),
        })

    # Extract system messages and convert OpenAI message format to Anthropic.
    system_parts: list[str] = []
    ant_messages: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            system_parts.append(m.get("content", ""))
        elif role == "assistant" and m.get("tool_calls"):
            # Convert OpenAI assistant tool_calls to Anthropic tool_use blocks.
            content_blocks: list[dict[str, Any]] = []
            # Round-trip thinking blocks from previous response (must come first).
            for tb in m.get("thinking_blocks") or []:
                content_blocks.append(tb)
            text = m.get("content")
            if text:
                content_blocks.append({"type": "text", "text": text})
            for tc in m["tool_calls"]:
                fn = tc.get("function", tc)
                tc_args = fn.get("arguments", {})
                if isinstance(tc_args, str):
                    try:
                        tc_args = json.loads(tc_args)
                    except (json.JSONDecodeError, TypeError):
                        tc_args = {}
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", tc.get("name", "")),
                    "input": tc_args,
                })
            ant_messages.append({"role": "assistant", "content": content_blocks})
        elif role == "tool":
            # Convert OpenAI tool result to Anthropic tool_result block.
            ant_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": m.get("content", ""),
                }],
            })
        else:
            ant_messages.append(m)
    system = "\n\n".join(system_parts)

    # Merge consecutive same-role messages (Anthropic requires alternating roles).
    merged: list[dict[str, Any]] = []
    for msg in ant_messages:
        if merged and merged[-1]["role"] == msg["role"]:
            prev_content = merged[-1]["content"]
            cur_content = msg["content"]
            # Normalize to list form for merging.
            if isinstance(prev_content, str):
                prev_content = [{"type": "text", "text": prev_content}]
            if isinstance(cur_content, str):
                cur_content = [{"type": "text", "text": cur_content}]
            if not isinstance(prev_content, list):
                prev_content = [prev_content]
            if not isinstance(cur_content, list):
                cur_content = [cur_content]
            merged[-1]["content"] = prev_content + cur_content
        else:
            merged.append(msg)

    kwargs: dict[str, Any] = {
        "model": model_cfg["model"],
        "max_tokens": max_tokens if max_tokens is not None else model_cfg.get("max_tokens", 4096),
        "messages": merged,
    }
    if system:
        kwargs["system"] = system
    if temperature is not None:
        kwargs["temperature"] = temperature
    elif model_cfg.get("temperature") is not None:
        kwargs["temperature"] = model_cfg["temperature"]
    if ant_tools:
        kwargs["tools"] = ant_tools
        # Convert OpenAI tool_choice format to Anthropic format.
        if tool_choice is not None and tool_choice != "auto":
            if isinstance(tool_choice, dict) and "function" in tool_choice:
                kwargs["tool_choice"] = {"type": "tool", "name": tool_choice["function"]["name"]}
            elif tool_choice == "none":
                kwargs["tool_choice"] = {"type": "none"}
            elif tool_choice == "required":
                kwargs["tool_choice"] = {"type": "any"}
            else:
                kwargs["tool_choice"] = {"type": "auto"}
        else:
            kwargs["tool_choice"] = {"type": "auto"}

    # Thinking configuration
    if model_cfg.get("enable_thinking"):
        kwargs["thinking"] = {"type": "adaptive"}
    reasoning_effort = model_cfg.get("reasoning_effort")
    if reasoning_effort:
        kwargs["output_config"] = {"effort": reasoning_effort}

    resp = await client.messages.create(**kwargs)

    text_parts: list[str] = []
    reasoning: Optional[str] = None
    tool_calls: list[dict[str, Any]] = []
    thinking_blocks: list[dict[str, Any]] = []
    for block in resp.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "thinking":
            reasoning = getattr(block, "thinking", None)
            # Preserve the full thinking block for round-tripping (includes signature).
            thinking_blocks.append(block.model_dump())
        elif block.type == "tool_use":
            tool_calls.append({
                "id": block.id,
                "name": block.name,
                "arguments": block.input if isinstance(block.input, dict) else {},
            })

    content = "\n\n".join(text_parts) if text_parts else None

    usage = None
    if resp.usage:
        usage = {
            "prompt_tokens": getattr(resp.usage, "input_tokens", 0) or 0,
            "completion_tokens": getattr(resp.usage, "output_tokens", 0) or 0,
        }

    return _normalize_response(content, tool_calls, reasoning, usage,
                               thinking_blocks=thinking_blocks or None)


def _sanitize_schema_for_gemini(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert OpenAI JSON Schema to Gemini-compatible format.

    Gemini's FunctionDeclaration does not support ``oneOf``, ``anyOf``,
    ``allOf``, or ``$ref``.  The most common case is nullable types
    represented as ``{"oneOf": [{"type": "X"}, {"type": "null"}]}``.
    Convert these to ``{"type": "X"}`` (drop the null variant).
    Recursively process nested properties and array items.
    """
    if not isinstance(schema, dict):
        return schema

    result = {}
    for key, value in schema.items():
        if key == "oneOf" and isinstance(value, list):
            # Extract the non-null type from oneOf nullable pattern
            non_null = [v for v in value if not (isinstance(v, dict) and v.get("type") == "null")]
            if len(non_null) == 1:
                return _sanitize_schema_for_gemini(non_null[0])
            # Multiple non-null types — fall back to first
            if non_null:
                return _sanitize_schema_for_gemini(non_null[0])
            continue
        elif key in ("anyOf", "allOf") and isinstance(value, list):
            # Take the first option
            if value:
                return _sanitize_schema_for_gemini(value[0])
            continue
        elif key == "$ref":
            continue
        elif key == "properties" and isinstance(value, dict):
            result[key] = {k: _sanitize_schema_for_gemini(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            result[key] = _sanitize_schema_for_gemini(value)
        else:
            result[key] = value
    return result


async def _call_gemini(
    model_cfg: dict[str, Any],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    temperature: Optional[float],
    max_tokens: Optional[int],
    tool_choice: Optional[Any] = None,
) -> dict[str, Any]:
    """Call the Gemini API via the native google-genai SDK."""
    from google.genai import types

    client = _get_gemini_client(model_cfg.get("api_key", ""))

    # --- Convert OpenAI-format tools to Gemini FunctionDeclaration ---
    gem_tools = None
    if tools:
        func_decls = []
        for t in tools:
            fn = t.get("function", t)
            params = fn.get("parameters", {})
            if params:
                params = _sanitize_schema_for_gemini(params)
            func_decls.append(types.FunctionDeclaration(
                name=fn.get("name", t.get("name", "")),
                description=fn.get("description", ""),
                parameters=params if params else None,
            ))
        gem_tools = [types.Tool(function_declarations=func_decls)]

    # --- Convert OpenAI-format messages to Gemini contents ---
    system_instruction = None
    gem_contents: list[types.Content] = []

    for m in messages:
        role = m.get("role")
        if role == "system":
            system_instruction = m.get("content", "")
        elif role == "user":
            content = m.get("content", "")
            gem_contents.append(types.Content(
                role="user",
                parts=[types.Part.from_text(text=content)],
            ))
        elif role == "assistant":
            parts: list[types.Part] = []
            # Round-trip thinking blocks from previous response.
            for tb in m.get("thinking_blocks") or []:
                parts.append(types.Part(thought=True, text=tb.get("text", "")))
            text = m.get("content")
            if text:
                parts.append(types.Part.from_text(text=text))
            # Convert tool_calls to FunctionCall parts.
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", tc)
                tc_args = fn.get("arguments", {})
                if isinstance(tc_args, str):
                    try:
                        tc_args = json.loads(tc_args)
                    except (json.JSONDecodeError, TypeError):
                        tc_args = {}
                parts.append(types.Part.from_function_call(
                    name=fn.get("name", tc.get("name", "")),
                    args=tc_args,
                ))
            if parts:
                gem_contents.append(types.Content(role="model", parts=parts))
        elif role == "tool":
            # Convert OpenAI tool result to Gemini FunctionResponse.
            tc_name = m.get("name", "")
            # Try to find the tool name from the preceding assistant message's tool_calls.
            if not tc_name:
                tc_id = m.get("tool_call_id", "")
                for prev_m in reversed(messages):
                    if prev_m.get("role") == "assistant":
                        for tc in prev_m.get("tool_calls") or []:
                            fn = tc.get("function", tc)
                            if tc.get("id") == tc_id:
                                tc_name = fn.get("name", tc.get("name", ""))
                                break
                        break
            result_content = m.get("content", "")
            try:
                result_obj = json.loads(result_content)
            except (json.JSONDecodeError, TypeError):
                result_obj = {"result": result_content}
            gem_contents.append(types.Content(
                role="user",
                parts=[types.Part.from_function_response(
                    name=tc_name,
                    response=result_obj,
                )],
            ))

    # --- Build generation config ---
    gen_config: dict[str, Any] = {}
    temp = temperature if temperature is not None else model_cfg.get("temperature", 0.7)
    if temp is not None:
        gen_config["temperature"] = temp
    mt = max_tokens if max_tokens is not None else model_cfg.get("max_tokens", 4096)
    if mt is not None:
        gen_config["max_output_tokens"] = mt

    # Thinking configuration
    thinking_config: dict[str, Any] = {}
    if model_cfg.get("enable_thinking"):
        thinking_config["include_thoughts"] = True
    reasoning_effort = model_cfg.get("reasoning_effort")
    thinking_budget = model_cfg.get("thinking_budget")
    if reasoning_effort:
        # Map reasoning_effort to thinkingLevel (Gemini 3.x) or thinkingBudget (2.5).
        # The SDK auto-selects the right param based on model.
        _effort_to_budget = {"low": 1024, "medium": 8192, "high": 24576}
        thinking_config["thinking_budget"] = _effort_to_budget.get(reasoning_effort, 8192)
    if thinking_budget is not None:
        thinking_config["thinking_budget"] = int(thinking_budget)
    if thinking_config:
        thinking_config.setdefault("include_thoughts", True)
        gen_config["thinking_config"] = types.ThinkingConfig(**thinking_config)

    config = types.GenerateContentConfig(**gen_config)
    if system_instruction:
        config.system_instruction = system_instruction
    if gem_tools:
        config.tools = gem_tools

    # --- Call API ---
    resp = await asyncio.to_thread(
        client.models.generate_content,
        model=model_cfg["model"],
        contents=gem_contents,
        config=config,
    )

    # --- Parse response ---
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    thinking_blocks: list[dict[str, Any]] = []
    reasoning: Optional[str] = None
    tc_counter = 0

    for candidate in resp.candidates:
        for part in candidate.content.parts:
            if getattr(part, "thought", False):
                thinking_blocks.append({"type": "thinking", "text": part.text or ""})
                reasoning = part.text
            elif part.function_call:
                fc = part.function_call
                tool_calls.append({
                    "id": f"call_{tc_counter}",
                    "name": fc.name,
                    "arguments": dict(fc.args) if fc.args else {},
                })
                tc_counter += 1
            elif part.text:
                text_parts.append(part.text)

    content = "\n\n".join(text_parts) if text_parts else None

    usage = None
    if resp.usage_metadata:
        usage = {
            "prompt_tokens": getattr(resp.usage_metadata, "prompt_token_count", 0) or 0,
            "completion_tokens": getattr(resp.usage_metadata, "candidates_token_count", 0) or 0,
        }

    return _normalize_response(content, tool_calls, reasoning, usage,
                               thinking_blocks=thinking_blocks or None)


async def _dispatch_llm_call(
    model_cfg: dict[str, Any],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    temperature: Optional[float],
    max_tokens: Optional[int],
    tool_choice: Optional[Any] = None,
) -> dict[str, Any]:
    """Route to the correct provider implementation."""
    provider = model_cfg.get("provider", "openai_compat")
    if provider in ("openai_compat", "openai"):
        return await _call_openai_compat(model_cfg, messages, tools, temperature, max_tokens, tool_choice)
    if provider == "gemini":
        return await _call_gemini(model_cfg, messages, tools, temperature, max_tokens, tool_choice)
    if provider == "anthropic":
        return await _call_anthropic(model_cfg, messages, tools, temperature, max_tokens, tool_choice)
    raise ValueError(f"Unsupported provider: {provider}")


# ---------------------------------------------------------------------------
# Retry + fallback logic
# ---------------------------------------------------------------------------


async def _call_with_retry(
    cfg: dict[str, Any],
    model_id: Optional[str],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    temperature: Optional[float],
    max_tokens: Optional[int],
    tool_choice: Optional[Any] = None,
    user_id: str = "not_specified",
) -> dict[str, Any]:
    """
    Call the LLM with retry and fallback.

    On failure, retries up to ``retry_count`` times with exponential backoff,
    then falls back to the model's ``fallback`` (or "default"), repeating
    until ``total_retry_count`` is exhausted.  Models not allowed for
    *user_id* are skipped during fallback.
    """
    retry_count = int(cfg.get("retry_count", 2))
    retry_interval = float(cfg.get("retry_interval", 1.0))
    retry_multiplier = float(cfg.get("retry_interval_multiplier", 2.0))
    total_limit = int(cfg.get("total_retry_count", 5))

    current_model_id = model_id or "default"

    # ACL gate on the initially requested model
    if not _is_model_allowed(cfg, current_model_id, user_id):
        logger.warning(
            "User '%s' not allowed model '%s', falling back to 'default'",
            user_id, current_model_id,
        )
        current_model_id = "default"

    total_attempts = 0
    visited: set[str] = set()

    while total_attempts < total_limit:
        model_cfg = _get_model_config(cfg, current_model_id)
        interval = retry_interval

        for attempt in range(retry_count + 1):
            total_attempts += 1
            if total_attempts > total_limit:
                break
            try:
                return await _dispatch_llm_call(
                    model_cfg, messages, tools, temperature, max_tokens, tool_choice,
                )
            except Exception as exc:
                logger.warning(
                    "LLM call failed (model=%s, attempt=%d/%d, total=%d/%d): %s",
                    current_model_id, attempt + 1, retry_count + 1,
                    total_attempts, total_limit, exc,
                )
                if attempt < retry_count and total_attempts < total_limit:
                    await asyncio.sleep(interval)
                    interval *= retry_multiplier

        # Switch to fallback model, skipping models not allowed for this user.
        visited.add(current_model_id)
        fallback = model_cfg.get("fallback")
        # Walk fallback chain until we find one the user can access
        while fallback and (fallback in visited or not _is_model_allowed(cfg, fallback, user_id)):
            if fallback in visited:
                break
            logger.info("Skipping fallback '%s' (not allowed for user '%s')", fallback, user_id)
            visited.add(fallback)
            fb_cfg = cfg.get("models", {}).get(fallback, {})
            fallback = fb_cfg.get("fallback")

        if not fallback or fallback in visited:
            if current_model_id != "default" and "default" not in visited:
                fallback = "default"
            else:
                break
        current_model_id = fallback
        logger.info("Falling back to model '%s'", current_model_id)

    raise RuntimeError(
        f"LLM call exhausted all retries ({total_limit}) across models: "
        f"{', '.join(visited | {current_model_id})}"
    )


# ---------------------------------------------------------------------------
# File download helper
# ---------------------------------------------------------------------------


_llm_pfm = ProxyFileManager(
    inbox_dir=Path(__file__).resolve().parent / "data" / "inbox",
    router_url=ROUTER_URL,
)


async def _read_proxy_file(proxy_file_dict: dict[str, Any]) -> str:
    """Fetch a ProxyFile to local disk and read its text content."""
    local_path = await _llm_pfm.fetch(proxy_file_dict)
    return Path(local_path).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


async def _run(data: dict[str, Any]) -> dict[str, Any]:
    """Process an inbound routing payload and return a result payload."""
    task_id: str = data.get("task_id", "")
    parent_task_id: Optional[str] = data.get("parent_task_id")
    raw_payload: dict[str, Any] = data.get("payload", {})

    cfg = _load_config()
    user_id: str = raw_payload.get("user_id") or "not_specified"

    # Determine input mode: LLMCall (new) or LLMData (legacy)
    llmcall_raw = raw_payload.get("llmcall")
    llmdata_raw = raw_payload.get("llmdata")

    # model_id can come from LLMCall, or top-level payload
    explicit_model_id: Optional[str] = raw_payload.get("model_id")

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tool_choice: Optional[Any] = None
    model_id: Optional[str] = explicit_model_id

    if llmcall_raw:
        # New LLMCall mode — raw messages + tools
        llmcall = LLMCall.model_validate(llmcall_raw)
        messages = llmcall.messages
        tools = llmcall.tools
        tool_choice = llmcall.tool_choice
        temperature = llmcall.temperature
        max_tokens = llmcall.max_tokens
        if llmcall.model_id and not model_id:
            model_id = llmcall.model_id
    elif llmdata_raw:
        # Legacy LLMData mode — convert prompt to messages
        if not llmdata_raw.get("prompt"):
            return build_result_request(
                agent_id=_OUR_AGENT_ID,
                task_id=task_id,
                parent_task_id=parent_task_id,
                status_code=400,
                output=AgentOutput(content="Error: payload.llmdata.prompt is required"),
            )

        llmdata = LLMData.model_validate(llmdata_raw)
        files_raw: list[dict[str, Any]] = raw_payload.get("files") or []

        system_parts: list[str] = []
        if llmdata.agent_instruction:
            system_parts.append(llmdata.agent_instruction)
        if llmdata.context:
            system_parts.append(llmdata.context)

        user_content = llmdata.prompt
        for f in files_raw:
            filename = f.get("path", "file").split("/")[-1]
            try:
                text = await _read_proxy_file(f)
                user_content += f"\n\n[File: {filename}]\n{text}"
            except Exception as exc:
                user_content += f"\n\n[File: {filename} — could not load: {exc}]"

        messages = []
        if system_parts:
            messages.append({"role": "system", "content": "\n\n".join(system_parts)})
        messages.append({"role": "user", "content": user_content})
        tools = []
    else:
        return build_result_request(
            agent_id=_OUR_AGENT_ID,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=400,
            output=AgentOutput(content="Error: payload must contain 'llmcall' or 'llmdata'"),
        )

    # Handle <list_model_id> special token — return available models without LLM call
    prompt_text = ""
    if llmdata_raw:
        prompt_text = llmdata_raw.get("prompt", "")
    elif llmcall_raw:
        # Check last user message
        for m in reversed(messages):
            if m.get("role") == "user":
                prompt_text = m.get("content", "") if isinstance(m.get("content"), str) else ""
                break
    if prompt_text.strip() == "<list_model_id>":
        allowed = _get_allowed_model_ids(cfg, user_id)
        models_info: dict[str, Any] = {}
        all_models = cfg.get("models", {})
        for mid in allowed:
            mcfg = all_models.get(mid, {})
            models_info[mid] = {
                "model": mcfg.get("model", ""),
                "provider": mcfg.get("provider", ""),
            }
        result_json = json.dumps({"user_id": user_id, "available_models": models_info}, ensure_ascii=False)
        return build_result_request(
            agent_id=_OUR_AGENT_ID,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=200,
            output=AgentOutput(content=result_json),
        )

    # Call LLM with retry + fallback
    result = await _call_with_retry(
        cfg, model_id, messages, tools, temperature, max_tokens, tool_choice,
        user_id=user_id,
    )

    # Return normalized response as JSON in AgentOutput.content
    return build_result_request(
        agent_id=_OUR_AGENT_ID,
        task_id=task_id,
        parent_task_id=parent_task_id,
        status_code=200,
        output=AgentOutput(content=json.dumps(result, ensure_ascii=False)),
    )


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="LLM Agent")


@app.post("/receive")
async def receive(request: Request) -> JSONResponse:
    """Called by the router via in-process ASGI transport."""
    data = await request.json()
    try:
        result = await _run(data)
        return JSONResponse(status_code=200, content=result)
    except Exception as exc:
        logger.exception("Unhandled error in llm_agent")
        task_id = data.get("task_id", "")
        parent_task_id = data.get("parent_task_id")
        error_payload = build_result_request(
            agent_id=_OUR_AGENT_ID,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=500,
            output=AgentOutput(content=f"LLM agent error: {exc}"),
        )
        return JSONResponse(status_code=200, content=error_payload)
