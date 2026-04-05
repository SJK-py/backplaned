"""
agent_info_builder.py — Derives AgentInfo from MCP tool definitions.

Builds the AgentInfo published to the router, generates tool summaries via
an LLM agent, and provides comprehensive documentation on demand.

Admin overrides (briefs and per-tool docs) take precedence over LLM output.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Optional

from mcp_manager import ToolDef

logger = logging.getLogger("mcp_agent")

MAX_DESCRIPTION_CHARS = 2000

# ---------------------------------------------------------------------------
# LLM prompt for brief generation
# ---------------------------------------------------------------------------

_BRIEF_PROMPT_TEMPLATE = """\
You are a technical writer. Given an MCP tool's name, description, and JSON \
Schema for its parameters, produce a ONE-LINE summary in this exact format:

<tool_name>(<param1>: <type>, [param2]: <type>, ...) — <what it does in ≤12 words>

Rules:
- List ALL parameters from the schema's "properties".
- Mark optional params with square brackets: [param]: type
- Required params have no brackets: param: type
- Use short type names: str, int, float, bool, list, dict, object.
- The description after the dash must be ≤12 words, no period at end.
- Output ONLY the single formatted line, nothing else.

Tool name: {name}
Description: {description}
Input schema:
```json
{schema}
```"""


_DOC_PROMPT_TEMPLATE = """\
You are a technical writer. Given an MCP tool's name, description, and JSON \
Schema for its parameters, write clear and comprehensive documentation in \
Markdown format.

Include these sections:
# {name}

## Overview
A 2-3 sentence explanation of what this tool does and when to use it.

## Parameters
A table or list of ALL parameters with:
- Name, type, required/optional, description, default value if any, \
and example values.

## Usage Examples
2-3 concrete JSON examples showing tool_name and arguments for common \
use cases. Use realistic values, not placeholders.

## Notes
Any caveats, rate limits, edge cases, or tips.

Tool name: {name}
Server description: {description}
Input schema:
```json
{schema}
```"""


def _build_brief_prompt(tool: ToolDef) -> str:
    """Build an LLM prompt to generate a brief tool summary."""
    return _BRIEF_PROMPT_TEMPLATE.format(
        name=tool.namespaced_name,
        description=tool.description,
        schema=json.dumps(tool.input_schema, indent=2),
    )


def _build_doc_prompt(tool: ToolDef) -> str:
    """Build an LLM prompt to generate comprehensive tool documentation."""
    return _DOC_PROMPT_TEMPLATE.format(
        name=tool.namespaced_name,
        description=tool.description,
        schema=json.dumps(tool.input_schema, indent=2),
    )


# ---------------------------------------------------------------------------
# ToolStore — unified persistence for briefs and per-tool documentation
# ---------------------------------------------------------------------------


class ToolStore:
    """
    Persists tool briefs and documentation to a single JSON file.

    Structure::

        {
          "briefs": {
            "<namespaced_name>:<schema_hash>": {
                "llm": "<LLM-generated brief>",
                "admin": "<admin override or null>"
            },
            ...
          },
          "docs": {
            "<namespaced_name>": "<admin-written documentation markdown>"
          }
        }

    ``get_brief()`` returns admin override if set, else LLM-generated.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._briefs: dict[str, dict[str, Optional[str]]] = {}
        self._docs: dict[str, dict[str, Optional[str]]] = {}
        self._load()

    # -- persistence --

    def _load(self) -> None:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                briefs_raw = raw.get("briefs", {})
                # Migrate old flat format: {"key": "string"} → {"key": {"llm": "string", "admin": null}}
                for k, v in briefs_raw.items():
                    if isinstance(v, str):
                        briefs_raw[k] = {"llm": v, "admin": None}
                self._briefs = briefs_raw
                docs_raw = raw.get("docs", {})
                # Migrate old flat format: {"name": "string"} → {"name": {"llm": null, "admin": "string"}}
                for k, v in docs_raw.items():
                    if isinstance(v, str):
                        docs_raw[k] = {"llm": None, "admin": v}
                self._docs = docs_raw
            except Exception:
                self._briefs = {}
                self._docs = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"briefs": self._briefs, "docs": self._docs}, indent=2),
            encoding="utf-8",
        )
        tmp.rename(self._path)

    # -- cache key --

    @staticmethod
    def _key(tool: ToolDef) -> str:
        schema_str = json.dumps(tool.input_schema, sort_keys=True)
        schema_hash = hashlib.sha256(schema_str.encode()).hexdigest()[:12]
        return f"{tool.namespaced_name}:{schema_hash}"

    # -- briefs --

    def _find_entry(self, tool: ToolDef) -> Optional[dict[str, Optional[str]]]:
        """Find the best matching entry: exact key first, then name prefix."""
        entry = self._briefs.get(self._key(tool))
        if entry is not None:
            return entry
        # Fallback: find any entry for this tool name (e.g. _manual key).
        for key, ent in self._briefs.items():
            if key.startswith(f"{tool.namespaced_name}:"):
                return ent
        return None

    def get_brief(self, tool: ToolDef) -> Optional[str]:
        """Return admin override if set, else LLM-generated brief."""
        entry = self._find_entry(tool)
        if entry is None:
            return None
        return entry.get("admin") or entry.get("llm")

    def get_brief_entry(self, tool: ToolDef) -> Optional[dict[str, Optional[str]]]:
        """Return the raw {"llm": ..., "admin": ...} entry."""
        return self._find_entry(tool)

    def has_llm_brief(self, tool: ToolDef) -> bool:
        entry = self._find_entry(tool)
        return entry is not None and bool(entry.get("llm"))

    def put_llm_brief(self, tool: ToolDef, brief: str) -> None:
        """Store an LLM-generated brief (preserves any existing admin override).

        If a placeholder entry (e.g. from set_admin_brief before LLM ran)
        exists for this tool, migrates the admin override to the real key
        and removes the placeholder.
        """
        key = self._key(tool)
        # Check for existing placeholder entry with admin override.
        admin_override: Optional[str] = None
        stale_keys: list[str] = []
        for k, entry in self._briefs.items():
            if k == key:
                continue
            if k.startswith(f"{tool.namespaced_name}:"):
                if entry.get("admin"):
                    admin_override = entry["admin"]
                stale_keys.append(k)
        for k in stale_keys:
            del self._briefs[k]

        existing = self._briefs.get(key, {"llm": None, "admin": None})
        existing["llm"] = brief
        if admin_override and not existing.get("admin"):
            existing["admin"] = admin_override
        self._briefs[key] = existing
        self._save()

    def set_admin_brief(self, tool_name: str, brief: Optional[str]) -> bool:
        """Set (or clear with None) the admin override for a tool brief by name.

        If no entry exists yet, creates one with a placeholder key.
        """
        for key, entry in self._briefs.items():
            if key.startswith(f"{tool_name}:"):
                entry["admin"] = brief
                self._save()
                return True
        # No entry yet — create one with placeholder hash.
        key = f"{tool_name}:_manual"
        self._briefs[key] = {"llm": None, "admin": brief}
        self._save()
        return True

    def get_brief_by_name(self, namespaced_name: str) -> Optional[str]:
        """Look up cached brief by tool name (ignoring schema hash)."""
        for key, entry in self._briefs.items():
            if key.startswith(f"{namespaced_name}:"):
                return entry.get("admin") or entry.get("llm")
        return None

    def prune(self, active_tools: set[str]) -> None:
        """Remove entries for tools that no longer exist."""
        stale_briefs = [k for k in self._briefs if k.split(":")[0] not in active_tools]
        stale_docs = [k for k in self._docs if k.split(":")[0] not in active_tools]
        if stale_briefs or stale_docs:
            for k in stale_briefs:
                del self._briefs[k]
            for k in stale_docs:
                del self._docs[k]
            self._save()

    # -- per-tool documentation --

    def get_doc(self, tool_name: str) -> Optional[str]:
        """Return admin doc if set, else LLM-generated doc."""
        entry = self._docs.get(tool_name)
        if entry is None:
            return None
        return entry.get("admin") or entry.get("llm")

    def get_doc_entry(self, tool_name: str) -> Optional[dict[str, Optional[str]]]:
        """Return the raw {"llm": ..., "admin": ...} doc entry."""
        return self._docs.get(tool_name)

    def has_llm_doc(self, tool_name: str) -> bool:
        entry = self._docs.get(tool_name)
        return entry is not None and bool(entry.get("llm"))

    def put_llm_doc(self, tool_name: str, doc: str) -> None:
        """Store an LLM-generated doc (preserves any existing admin override)."""
        existing = self._docs.get(tool_name, {"llm": None, "admin": None})
        existing["llm"] = doc
        self._docs[tool_name] = existing
        self._save()

    def set_doc(self, tool_name: str, doc: Optional[str]) -> None:
        """Set (or clear with None) admin documentation for a tool."""
        existing = self._docs.get(tool_name, {"llm": None, "admin": None})
        existing["admin"] = doc
        self._docs[tool_name] = existing
        if not existing.get("llm") and not existing.get("admin"):
            self._docs.pop(tool_name, None)
        self._save()

    # -- bulk queries for admin UI --

    def get_all_overrides(self) -> dict[str, dict[str, Any]]:
        """Return a summary of all stored data keyed by namespaced_name."""
        result: dict[str, dict[str, Any]] = {}
        for key, entry in self._briefs.items():
            name = key.split(":")[0]
            result.setdefault(name, {})
            result[name]["llm_brief"] = entry.get("llm")
            result[name]["admin_brief"] = entry.get("admin")
        for name, entry in self._docs.items():
            result.setdefault(name, {})
            result[name]["llm_doc"] = entry.get("llm")
            result[name]["admin_doc"] = entry.get("admin")
        return result


# Backward compat alias
BriefCache = ToolStore


# ---------------------------------------------------------------------------
# AgentInfo derivation
# ---------------------------------------------------------------------------


def derive_agent_info(
    agent_id: str,
    tools: list[ToolDef],
    tool_store: Optional[ToolStore] = None,
) -> dict[str, Any]:
    """
    Build an AgentInfo dict from the combined MCP tool list.

    If a ToolStore is provided and contains briefs, those are used in
    the description. Otherwise falls back to auto-generated summaries.
    """
    if not tools:
        return {
            "agent_id": agent_id,
            "description": (
                "MCP tool gateway (external tool bridge). No tools currently connected. "
                "Connect MCP servers to expose tools."
            ),
            "input_schema": "tool_name: str, arguments: dict",
            "output_schema": "content: str",
            "required_input": ["tool_name", "arguments"],
        }

    # Build per-tool summaries for the description field.
    tool_lines: list[str] = []
    for t in tools:
        brief = tool_store.get_brief(t) if tool_store else None
        if brief:
            tool_lines.append(brief)
        else:
            # Auto-generate from schema (no LLM needed).
            tool_lines.append(_auto_brief(t))

    tools_block = "\n".join(tool_lines)
    description = (
        f"MCP tool gateway — bridges {len(tools)} external tools from connected MCP servers. "
        "Call with tool_name and arguments dict. "
        'Use tool_name="details" arguments={{"tool": "<name>"}} to get full schema before calling.\n'
        f"Available tools:\n{tools_block}"
    )

    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[: MAX_DESCRIPTION_CHARS - 3] + "..."

    return {
        "agent_id": agent_id,
        "description": description,
        "input_schema": "tool_name: str, arguments: dict",
        "output_schema": "content: str",
        "required_input": ["tool_name", "arguments"],
    }


def _auto_brief(tool: ToolDef) -> str:
    """Generate a brief from the schema without LLM (fallback)."""
    props = tool.input_schema.get("properties", {})
    required = set(tool.input_schema.get("required", []))
    params: list[str] = []
    for name, prop in props.items():
        ptype = prop.get("type", "any")
        if name in required:
            params.append(f"{name}: {ptype}")
        else:
            params.append(f"[{name}]: {ptype}")
    params_str = ", ".join(params) if params else ""
    desc_short = tool.description[:50] + ("..." if len(tool.description) > 50 else "")
    return f"{tool.namespaced_name}({params_str}) — {desc_short}"


def get_uncached_tools(
    tools: list[ToolDef],
    tool_store: ToolStore,
) -> list[ToolDef]:
    """Return tools that don't yet have an LLM-generated brief."""
    return [t for t in tools if not tool_store.has_llm_brief(t)]


def get_uncached_doc_tools(
    tools: list[ToolDef],
    tool_store: ToolStore,
) -> list[ToolDef]:
    """Return tools that don't yet have an LLM-generated doc."""
    return [t for t in tools if not tool_store.has_llm_doc(t.namespaced_name)]


# ---------------------------------------------------------------------------
# Comprehensive documentation (on-demand)
# ---------------------------------------------------------------------------


def build_tool_detail(
    tool: ToolDef,
    tool_store: Optional[ToolStore] = None,
) -> str:
    """Build comprehensive documentation for a single tool."""
    # Admin/LLM doc takes precedence if present.
    if tool_store:
        doc = tool_store.get_doc(tool.namespaced_name)
        if doc:
            return doc

    brief = tool_store.get_brief(tool) if tool_store else None
    lines = [
        f"# {tool.namespaced_name}",
        "",
    ]
    if brief:
        lines.append(f"**Summary:** {brief}")
        lines.append("")
    lines.extend([
        f"**Server:** {tool.server_name}",
        f"**Raw name:** {tool.name}",
        "",
        "## Description",
        "",
        tool.description,
        "",
        "## Input Schema",
        "",
        "```json",
        json.dumps(tool.input_schema, indent=2),
        "```",
        "",
        "## Usage",
        "",
        "```json",
        json.dumps({
            "tool_name": tool.namespaced_name,
            "arguments": _build_example_args(tool.input_schema),
        }, indent=2),
        "```",
    ])
    return "\n".join(lines)


def build_tools_markdown(
    tools: list[ToolDef],
    tool_store: Optional[ToolStore] = None,
) -> str:
    """Generate Markdown documentation listing all available MCP tools."""
    if not tools:
        return "# MCP Tools\n\nNo tools currently available.\n"

    lines: list[str] = [
        "# MCP Tools",
        "",
        f"Total tools: {len(tools)}",
        "",
    ]

    by_server: dict[str, list[ToolDef]] = {}
    for t in tools:
        by_server.setdefault(t.server_name, []).append(t)

    for server_name, server_tools in sorted(by_server.items()):
        lines.append(f"## Server: {server_name}")
        lines.append("")

        for t in sorted(server_tools, key=lambda x: x.name):
            brief = tool_store.get_brief(t) if tool_store else None
            lines.append(f"### `{t.namespaced_name}`")
            lines.append("")
            if brief:
                lines.append(f"**Brief:** {brief}")
                lines.append("")
            lines.append(t.description)
            lines.append("")
            lines.append("**Input Schema:**")
            lines.append("```json")
            lines.append(json.dumps(t.input_schema, indent=2))
            lines.append("```")
            lines.append("")

    return "\n".join(lines)


def _build_example_args(schema: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal example arguments dict from a JSON Schema."""
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    example: dict[str, Any] = {}
    for name, prop in props.items():
        if name not in required:
            continue
        ptype = prop.get("type", "string")
        if ptype == "string":
            example[name] = f"<{name}>"
        elif ptype == "integer":
            example[name] = 0
        elif ptype == "number":
            example[name] = 0.0
        elif ptype == "boolean":
            example[name] = True
        elif ptype == "array":
            example[name] = []
        elif ptype == "object":
            example[name] = {}
        else:
            example[name] = f"<{name}>"
    return example
