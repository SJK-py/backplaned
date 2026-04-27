"""bp_sdk.tools — Build provider-specific tool schemas from AgentInfo.

Replaces helper.py:1051-1156 (build_anthropic_tools / build_openai_tools).
Provider adapters register here; new providers are added by registering
a `ToolFormatAdapter`, not by forking a function.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Literal


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------


ToolFormatAdapter = Callable[[dict[str, Any]], list[dict[str, Any]]]
"""Builds the provider-specific tool schemas from `available_destinations`.

Input dict shape from `WelcomeFrame.available_destinations`:
    { agent_id: { description, capabilities, tags, accepts_schema, hidden, ... }, ... }
"""


_ADAPTERS: dict[str, ToolFormatAdapter] = {}


def register_provider(name: str, adapter: ToolFormatAdapter) -> None:
    _ADAPTERS[name] = adapter


def build_tools(
    destinations: dict[str, Any],
    *,
    provider: Literal["anthropic", "openai", "gemini"],
) -> list[dict[str, Any]]:
    """Build tool definitions for an LLM call. Hidden agents are excluded."""
    visible = {k: v for k, v in destinations.items() if not v.get("hidden")}
    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        raise ValueError(f"unknown provider: {provider!r}")
    return adapter(visible)


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


def _parameters_schema(entry: dict[str, Any]) -> dict[str, Any]:
    """Pull the JSON Schema for an agent's input. Falls back to a permissive
    object if accepts_schema is absent.
    """
    schema = entry.get("accepts_schema")
    if isinstance(schema, dict) and schema.get("type") == "object":
        return schema
    return {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    }


def _safe_tool_name(agent_id: str) -> str:
    """call_<agent_id> with characters scrubbed for provider name rules.

    Most providers require [A-Za-z0-9_-]{,64}. Replace illegal chars
    with '_'. Truncate to a sensible length.
    """
    raw = f"call_{agent_id}"
    return re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:64]


def _description(entry: dict[str, Any]) -> str:
    desc = entry.get("description", "")
    caps = entry.get("capabilities") or []
    if caps:
        desc += f" [capabilities: {', '.join(caps)}]"
    return desc


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def _anthropic_adapter(destinations: dict[str, Any]) -> list[dict[str, Any]]:
    """Anthropic Messages API tool format:
        { name, description, input_schema: <json schema> }
    """
    return [
        {
            "name": _safe_tool_name(agent_id),
            "description": _description(entry),
            "input_schema": _parameters_schema(entry),
        }
        for agent_id, entry in destinations.items()
    ]


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


def _openai_adapter(destinations: dict[str, Any]) -> list[dict[str, Any]]:
    """OpenAI Chat Completions tool format:
        { type: "function", function: { name, description, parameters } }
    """
    return [
        {
            "type": "function",
            "function": {
                "name": _safe_tool_name(agent_id),
                "description": _description(entry),
                "parameters": _parameters_schema(entry),
            },
        }
        for agent_id, entry in destinations.items()
    ]


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def _gemini_adapter(destinations: dict[str, Any]) -> list[dict[str, Any]]:
    """Gemini function-declarations format. The Google client expects:

        { function_declarations: [
            { name, description, parameters: <subset of JSON Schema> }
          ]
        }

    Multiple agents are bundled into a single function_declarations
    array, returned as a single-element list so the caller can append
    other tool blocks (e.g. {google_search:{}}).
    """
    declarations = [
        {
            "name": _safe_tool_name(agent_id),
            "description": _description(entry),
            "parameters": _gemini_strip_schema(_parameters_schema(entry)),
        }
        for agent_id, entry in destinations.items()
    ]
    if not declarations:
        return []
    return [{"function_declarations": declarations}]


_GEMINI_ALLOWED_KEYS = {
    "type",
    "format",
    "description",
    "nullable",
    "enum",
    "items",
    "properties",
    "required",
    "minimum",
    "maximum",
    "min_items",
    "max_items",
    "min_length",
    "max_length",
}


def _gemini_strip_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Gemini's function-declaration parameters accept only a subset of
    JSON Schema. Drop unsupported keys recursively."""
    if not isinstance(schema, dict):
        return schema  # type: ignore[return-value]
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k not in _GEMINI_ALLOWED_KEYS:
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: _gemini_strip_schema(pv) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            out[k] = _gemini_strip_schema(v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


register_provider("anthropic", _anthropic_adapter)
register_provider("openai", _openai_adapter)
register_provider("gemini", _gemini_adapter)
