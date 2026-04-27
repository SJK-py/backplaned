"""bp_sdk.tools — Build provider-specific tool schemas from AgentInfo.

Replaces helper.py:1051-1156 (build_anthropic_tools / build_openai_tools).
Provider adapters register here; new providers are added by registering
a `ToolFormatAdapter`, not by forking a function.
"""

from __future__ import annotations

from typing import Any, Callable, Literal


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------


ToolFormatAdapter = Callable[[dict[str, Any]], list[dict[str, Any]]]
"""Builds the provider-specific tool schemas from `available_destinations`.

Input dict has the shape exposed in `WelcomeFrame.available_destinations`:
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
    """Build tool definitions for an LLM call.

    Hidden agents (`AgentInfo.hidden=True`) are excluded.
    """
    visible = {k: v for k, v in destinations.items() if not v.get("hidden")}
    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        raise ValueError(f"unknown provider: {provider!r}")
    return adapter(visible)


# ---------------------------------------------------------------------------
# Built-in adapters (skeletons)
# ---------------------------------------------------------------------------


def _anthropic_adapter(destinations: dict[str, Any]) -> list[dict[str, Any]]:
    """Anthropic tool-use format. Implementation pending."""
    raise NotImplementedError


def _openai_adapter(destinations: dict[str, Any]) -> list[dict[str, Any]]:
    """OpenAI function-calling format. Implementation pending."""
    raise NotImplementedError


def _gemini_adapter(destinations: dict[str, Any]) -> list[dict[str, Any]]:
    """Gemini function-declarations format. Implementation pending."""
    raise NotImplementedError


register_provider("anthropic", _anthropic_adapter)
register_provider("openai", _openai_adapter)
register_provider("gemini", _gemini_adapter)
