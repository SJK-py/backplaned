"""
agents/core_personal_agent/agent.py — Core personal LLM agent (embedded).

Receives user messages from the channel agent, maintains per-session
chat history, runs a full LLM agent loop with parallel tool-calling, and
manages long-term memory via memory_agent.

Input payload schema:
    user_id:    str  — identifies the user
    session_id: str  — identifies the chat session
    message:    str  — user message or system control token

Control tokens (message field):
    <new_session> [sid]     Archive history and reset session.
                            Optional sid records new active session for user.
    <token_info>            Return estimated token usage for this session.
    <agents_info>           Return descriptions of available agents.
    <user_config> <text>    Write <text> to user_id.md for this user.

Parallel tool-call design
--------------------------
When the LLM returns multiple tool calls in one turn, each is spawned as a
new router task via authenticated HTTP POST to /route (with 1 s between
spawns as per spec).  Results are delivered back by the router via new ASGI
calls to this agent's /receive endpoint.  Those calls set asyncio.Future
objects that the main loop is awaiting, allowing concurrent resolution within
the same asyncio event loop.  The /receive handler returns JSON null for
result-delivery calls so the router skips _process_route_internal.

Note on timeouts
-----------------
The router's embedded-agent ASGI call has a hard timeout (default 60 s in
_deliver_embedded).  CORE_AGENT_TIMEOUT must be below that limit.  For
models or tasks that need longer, increase the router's ASGI timeout and
CORE_AGENT_TIMEOUT accordingly.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import mimetypes
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import logging

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from helper import (
    AgentInfo,
    AgentOutput,
    ProxyFile,
    ProxyFileManager,
    build_openai_tools,
    build_result_request,
    build_spawn_request,
    extract_result_text,
    handle_fetch_agent_documentation,
    push_progress_direct,
)

# ---------------------------------------------------------------------------
# Configuration — read directly from config.json (hot-reloadable)
# ---------------------------------------------------------------------------

_AGENT_DIR = str(Path(__file__).resolve().parent)
_CONFIG_PATH = Path(_AGENT_DIR) / "data" / "config.json"

_DEFAULT_SYSTEM_PROMPT: str = (
    "You are a personal assistant in a multi-agent system. You orchestrate tasks "
    "by calling specialized agents (web search, coding, knowledge base, etc.) and "
    "synthesize their results for the user.\n\n"
    "## Guidelines\n"
    "- Use search_memory to recall user preferences and past context before answering.\n"
    "- When delegating to other agents, provide comprehensive context in the prompt — "
    "they have no access to this conversation's history.\n"
    "- Synthesize agent results into clear, complete responses with key details preserved.\n\n"
    "## Delegating to agents\n"
    "- Provide the task in llmdata.prompt with full background in llmdata.context.\n"
    "- Each agent has its OWN isolated file storage. Agents CANNOT access your "
    "inbox or each other's files. File transfer between agents happens "
    "automatically via the files argument and attach_file mechanism.\n"
    "- When instructing agents to produce files, say 'create X and attach it' — "
    "NEVER say 'save to path', 'output the file path', or 'return the file path'. "
    "Agents use attach_file internally, and you receive the file automatically.\n"
    "- NEVER include any file paths (inbox/..., /workspace/..., etc.) in prompts "
    "to other agents. Pass existing files via the files argument instead.\n\n"
    "## File handling\n"
    "- Your inbox is private. Files appear via [Attached files:] or "
    "[Result files:] blocks, shown as filenames.\n"
    "- Use just the filename when calling file tools (read_file_text, "
    "attach_file, write_file, delete_file). The inbox path is resolved automatically.\n"
    "- To forward files to another agent, pass filenames in the files argument "
    "(automatically resolved and transferred).\n"
    "- To send a file to the user, call attach_file with the filename."
)


def _load_config() -> dict[str, Any]:
    """Read config.json from disk (re-read on every call for hot-reload)."""
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cfg(key: str, default: str = "") -> str:
    """Get a config value as string, with fallback default.

    Empty-string values in config.json are treated as unset so that code
    defaults take effect.
    """
    val = str(_load_config().get(key, default))
    return val if val else default


# Router URL — from the router process environment (set via root .env).
ROUTER_URL: str = os.environ.get("ROUTER_URL", "http://localhost:8000").rstrip("/")

# Module-level config variables — refreshed on each /receive call.
AGENT_TIMEOUT: float = 290.0
TOOL_TIMEOUT: float = 240.0
LLM_AGENT_ID: str = "llm_agent"
LLM_MODEL_ID: Optional[str] = None
HISTORY_TOKEN_LIMIT: int = 8000
MEMORY_AGENT_ID: str = "memory_agent"
MAX_AGENT_ITERATIONS: int = 25
LINK_HISTORY_TOKEN_RATIO: float = 0.5
LINK_TRUNCATION_KEEP_RATIO: float = 0.5
SYSTEM_PROMPT: str = _DEFAULT_SYSTEM_PROMPT

# Directory paths — initialised once at import (not hot-reloadable).
HISTORY_DIR: str = _cfg("CORE_HISTORY_DIR", f"{_AGENT_DIR}/data/sessions")
HISTORY_ARCHIVE_DIR: str = _cfg("CORE_HISTORY_ARCHIVE_DIR", f"{_AGENT_DIR}/data/sessions/archive")
USER_CONFIG_DIR: str = _cfg("CORE_USER_CONFIG_DIR", f"{_AGENT_DIR}/data/users")

# Ensure storage directories exist at module load time.
for _d in [HISTORY_DIR, HISTORY_ARCHIVE_DIR, USER_CONFIG_DIR]:
    Path(_d).mkdir(parents=True, exist_ok=True)


def _refresh_config() -> None:
    """Re-read config.json and update module-level variables."""
    global AGENT_TIMEOUT, TOOL_TIMEOUT, LLM_AGENT_ID, LLM_MODEL_ID
    global HISTORY_TOKEN_LIMIT, MEMORY_AGENT_ID, MAX_AGENT_ITERATIONS
    global LINK_HISTORY_TOKEN_RATIO, LINK_TRUNCATION_KEEP_RATIO, SYSTEM_PROMPT
    cfg = _load_config()
    _si = lambda v, d: d if v is None or v == "" else int(v)
    _sf = lambda v, d: d if v is None or v == "" else float(v)
    AGENT_TIMEOUT = _sf(cfg.get("CORE_AGENT_TIMEOUT"), 290.0)
    TOOL_TIMEOUT = _sf(cfg.get("CORE_TOOL_TIMEOUT"), 240.0)
    LLM_AGENT_ID = str(cfg.get("CORE_LLM_AGENT_ID") or "llm_agent")
    LLM_MODEL_ID = str(cfg.get("CORE_LLM_MODEL_ID") or "") or None
    HISTORY_TOKEN_LIMIT = _si(cfg.get("CORE_HISTORY_TOKEN_LIMIT"), 8000)
    MEMORY_AGENT_ID = str(cfg.get("CORE_MEMORY_AGENT_ID") or "memory_agent")
    MAX_AGENT_ITERATIONS = _si(cfg.get("CORE_MAX_AGENT_ITERATIONS"), 25)
    LINK_HISTORY_TOKEN_RATIO = _sf(cfg.get("CORE_LINK_HISTORY_TOKEN_RATIO"), 0.5)
    LINK_TRUNCATION_KEEP_RATIO = _sf(cfg.get("CORE_LINK_TRUNCATION_KEEP_RATIO"), 0.5)
    SYSTEM_PROMPT = str(cfg.get("CORE_SYSTEM_PROMPT") or _DEFAULT_SYSTEM_PROMPT)


_refresh_config()  # Initial load

# ---------------------------------------------------------------------------
# AgentInfo
# ---------------------------------------------------------------------------

AGENT_INFO = AgentInfo(
    agent_id="core_personal_agent",
    description=(
        "Personal assistant orchestrator. Maintains chat history and long-term memory. "
        "Delegates tasks to specialized agents. Not typically called by other agents."
    ),
    input_schema="user_id: str, session_id: str, message: str, files: Optional[List[ProxyFile]]",
    output_schema="content: str, files: Optional[List[ProxyFile]]",
    required_input=["user_id", "session_id", "message"],
)

_OUR_AGENT_ID = "core_personal_agent"

# ---------------------------------------------------------------------------
# Active loop state (asyncio.Future wiring across concurrent /receive calls)
# ---------------------------------------------------------------------------

@dataclass
class _LoopState:
    """
    Tracks the in-flight agent loop for one task invocation.

    pending maps router-assigned identifiers to asyncio.Futures.  When a
    result delivery arrives via a new /receive call, the future is resolved,
    allowing the waiting agent loop to continue.
    """
    task_id: str
    parent_task_id: Optional[str]
    session_id: str
    user_id: str
    pending: dict[str, "asyncio.Future[dict[str, Any]]"] = field(default_factory=dict)
    cancelled: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0


# Global registry: task_id → _LoopState (only entries for active invocations)
_active_loops: dict[str, _LoopState] = {}

# ---------------------------------------------------------------------------
# Auth token (injected by the router as an environment variable at startup)
# ---------------------------------------------------------------------------

_AUTH_TOKEN: str = ""


def _get_auth_token() -> str:
    global _AUTH_TOKEN
    if not _AUTH_TOKEN:
        _AUTH_TOKEN = os.environ.get(f"{_OUR_AGENT_ID.upper()}_AUTH_TOKEN", "")
    return _AUTH_TOKEN


# ---------------------------------------------------------------------------
# Session history helpers
# ---------------------------------------------------------------------------

def _history_path(session_id: str) -> Path:
    return Path(HISTORY_DIR) / f"{session_id}.json"


def _load_history(session_id: str) -> list[dict]:
    p = _history_path(session_id)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_history(session_id: str, history: list[dict]) -> None:
    target = _history_path(session_id)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(str(tmp), str(target))


def _archive_and_clear(session_id: str) -> None:
    """Move the session history, tool history, link state, and inbox to the archive directory."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    p = _history_path(session_id)
    if p.exists():
        dest = Path(HISTORY_ARCHIVE_DIR) / f"{session_id}_{ts}.json"
        shutil.move(str(p), str(dest))
    tp = _tool_history_path(session_id)
    if tp.exists():
        dest = Path(HISTORY_ARCHIVE_DIR) / f"{session_id}_{ts}_tools.json"
        shutil.move(str(tp), str(dest))
    lp = _link_state_path(session_id)
    if lp.exists():
        dest = Path(HISTORY_ARCHIVE_DIR) / f"{session_id}_{ts}_link.json"
        shutil.move(str(lp), str(dest))
    # Clean up per-session inbox
    inbox = _INBOX_BASE / session_id
    if inbox.exists():
        shutil.rmtree(str(inbox), ignore_errors=True)


def _history_tokens(history: list[dict]) -> int:
    """Rough token estimate: serialised bytes // 4."""
    return len(json.dumps(history)) // 4


def _history_to_transcript(history: list[dict], user_id: str | None = None) -> str:
    lines = []
    for e in history:
        role = e.get("role", "user")
        if user_id and role == "user":
            label = user_id
        elif role == "assistant":
            label = "AI agent"
        else:
            label = role
        content = e.get("content", "")
        if isinstance(content, list):
            # Extract text parts from multimodal content blocks.
            content = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        lines.append(f"[{label}] {content or ''}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool-call history (separate per-session log of all tool invocations)
# ---------------------------------------------------------------------------

def _tool_history_path(session_id: str) -> Path:
    return Path(HISTORY_DIR) / f"{session_id}_tools.json"


def _load_tool_history(session_id: str) -> list[dict]:
    p = _tool_history_path(session_id)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _append_tool_history(
    session_id: str,
    turn: int,
    tool_calls: list["_ToolCall"],
    tc_results: dict[str, str],
) -> None:
    """Append one turn of tool calls with results to the session tool history."""
    entries = _load_tool_history(session_id)
    ts = datetime.now(timezone.utc).isoformat()
    for tc in tool_calls:
        result = tc_results.get(tc.id, "")
        # Truncate large results to keep file manageable
        preview = result[:1500] + "…" if len(result) > 1500 else result
        entries.append({
            "ts": ts,
            "turn": turn,
            "tool": tc.name,
            "arguments": tc.arguments,
            "result_preview": preview,
            "status": "error" if result.startswith("Error") else "ok",
        })
    _tool_history_path(session_id).write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Linked-mode state (per-session, persisted to disk)
# ---------------------------------------------------------------------------

def _link_state_path(session_id: str) -> Path:
    return Path(HISTORY_DIR) / f"{session_id}_link.json"


def _load_link_state(session_id: str) -> Optional[dict[str, Any]]:
    p = _link_state_path(session_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_link_state(session_id: str, state: dict[str, Any]) -> None:
    _link_state_path(session_id).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _clear_link_state(session_id: str) -> None:
    p = _link_state_path(session_id)
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# Active session tracking — maps user_id → most recent session_id
# ---------------------------------------------------------------------------

_ACTIVE_SESSIONS_PATH = Path(HISTORY_DIR) / "active_sessions.json"


def _load_active_sessions() -> dict[str, str]:
    if _ACTIVE_SESSIONS_PATH.exists():
        try:
            return json.loads(_ACTIVE_SESSIONS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_active_sessions(m: dict[str, str]) -> None:
    _ACTIVE_SESSIONS_PATH.write_text(
        json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _set_active_session(user_id: str, session_id: str) -> None:
    """Record that session_id is the most recent session for user_id."""
    m = _load_active_sessions()
    m[user_id] = session_id
    _save_active_sessions(m)


def _resolve_session(user_id: str, session_id: str) -> tuple[str, Optional[str]]:
    """
    If session_id is not the user's active session, return the active one.

    Returns (resolved_session_id, original_session_id_if_changed).
    If the session_id is already active (or no active session is recorded),
    returns (session_id, None).
    """
    m = _load_active_sessions()
    active = m.get(user_id)
    if active and active != session_id:
        return active, session_id
    return session_id, None



# ---------------------------------------------------------------------------
# Per-user JSON config (model_id, history_token_limit, etc.)
# ---------------------------------------------------------------------------

_DEFAULT_USER_JSON_CONFIG: dict[str, Any] = {
    "model_id": None,
    "summarization_model_id": None,
    "history_token_limit": None,
    "memory_agent_id": None,
    "system_prompt": None,
    "timezone": None,
    "user_config_md": "",
}


def _user_json_config_path(user_id: str) -> Path:
    return Path(USER_CONFIG_DIR) / f"{user_id}.config.json"


def _load_user_json_config(user_id: str) -> dict[str, Any]:
    """Load per-user JSON config, merged with defaults."""
    cfg = dict(_DEFAULT_USER_JSON_CONFIG)
    p = _user_json_config_path(user_id)
    if p.exists():
        try:
            stored = json.loads(p.read_text(encoding="utf-8"))
            cfg.update(stored)
        except Exception:
            pass
    return cfg


def _save_user_json_config(user_id: str, cfg: dict[str, Any]) -> None:
    """Save per-user JSON config."""
    Path(USER_CONFIG_DIR).mkdir(parents=True, exist_ok=True)
    p = _user_json_config_path(user_id)
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def _resolve_summarization_model(user_id: str) -> Optional[str]:
    """Resolve the model to use for summarization tasks (briefs, context consolidation).

    Falls back: summarization_model_id → model_id → LLM_MODEL_ID.
    """
    cfg = _load_user_json_config(user_id)
    return cfg.get("summarization_model_id") or cfg.get("model_id") or LLM_MODEL_ID


# ---------------------------------------------------------------------------
# Normalised LLM types
# ---------------------------------------------------------------------------

@dataclass
class _ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class _LLMResponse:
    content: Optional[str]
    tool_calls: list[_ToolCall]
    usage: Optional[dict[str, int]] = None
    thinking_blocks: Optional[list[dict[str, Any]]] = None


# ---------------------------------------------------------------------------
# LLM inference via llm_agent (spawned through the router)
# ---------------------------------------------------------------------------

async def _llm_call(
    messages: list[dict],
    tools: list[dict],
    loop_state: "_LoopState",
    model_id: Optional[str] = None,
    tool_choice: Optional[Any] = None,
) -> _LLMResponse:
    """
    Call the centralized llm_agent and return a normalised _LLMResponse.

    Spawns a task to llm_agent with an LLMCall payload, waits for the result
    via the existing identifier/future mechanism, and parses the normalized
    JSON response.
    """
    identifier = f"llm_{uuid.uuid4().hex[:12]}"
    running_loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = running_loop.create_future()
    loop_state.pending[identifier] = fut

    llmcall_payload: dict[str, Any] = {
        "messages": messages,
        "tools": tools,
        "model_id": model_id or LLM_MODEL_ID,
    }
    if tool_choice is not None:
        llmcall_payload["tool_choice"] = tool_choice

    payload: dict[str, Any] = {
        "llmcall": llmcall_payload,
        "user_id": loop_state.user_id,
    }

    await _spawn_via_http(
        identifier=identifier,
        parent_task_id=loop_state.task_id,
        dest=LLM_AGENT_ID,
        payload=payload,
        pending=loop_state.pending,
    )

    try:
        result_data = await asyncio.wait_for(fut, timeout=TOOL_TIMEOUT)
    except asyncio.TimeoutError:
        loop_state.pending.pop(identifier, None)
        raise RuntimeError("LLM call timed out waiting for llm_agent response")
    finally:
        loop_state.pending.pop(identifier, None)

    # Parse the normalized JSON response from llm_agent
    raw_payload = result_data.get("payload", {})
    status_code = result_data.get("status_code", 200)
    content_str = raw_payload.get("content", "")

    if status_code and status_code >= 400:
        raise RuntimeError(f"llm_agent returned error ({status_code}): {content_str}")

    try:
        parsed = json.loads(content_str)
    except (json.JSONDecodeError, TypeError):
        # If content is not JSON, treat as plain text response
        return _LLMResponse(content=content_str, tool_calls=[])

    tcs: list[_ToolCall] = []
    for tc_raw in parsed.get("tool_calls", []):
        tcs.append(_ToolCall(
            id=tc_raw.get("id", uuid.uuid4().hex[:8]),
            name=tc_raw.get("name", ""),
            arguments=tc_raw.get("arguments", {}),
        ))
    return _LLMResponse(content=parsed.get("content"), tool_calls=tcs, usage=parsed.get("usage"),
                        thinking_blocks=parsed.get("thinking_blocks"))


# ---------------------------------------------------------------------------
# Tool list builder
# ---------------------------------------------------------------------------

# search_memory is a hand-crafted tool; memory_agent is excluded from the
# auto-built tool list to prevent the LLM from calling memory_add directly.
_SEARCH_MEMORY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_memory",
        "description": (
            "Search long-term memory for information about the current user. "
            "Returns a JSON array of relevant memory entries. Use this to recall "
            "user preferences, past context, or any information previously shared."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "count": {
                    "type": "integer",
                    "description": "Maximum number of memories to return (default 5).",
                },
            },
            "required": ["query"],
        },
    },
}


_READ_IMAGE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_image",
        "description": "Read an image file for visual analysis.",
        "parameters": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "Filename (e.g. 'photo.jpg').",
                },
            },
            "required": ["file"],
        },
    },
}

_READ_FILE_TEXT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_file_text",
        "description": "Read the text content of a file. Works with .txt, .md, .json, .csv, .py, etc.",
        "parameters": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "Filename (e.g. 'notes.txt').",
                },
            },
            "required": ["file"],
        },
    },
}

_SHOW_TOOL_HISTORY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "show_tool_history",
        "description": (
            "Retrieve the tool-call history for the current session. Returns a "
            "chronological list of every tool invocation (name, arguments, result "
            "preview, status). Use this to recall what tools were called earlier "
            "in the conversation, including their results."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Optional: filter to only show calls to this tool.",
                },
                "last_n": {
                    "type": "integer",
                    "description": "Optional: return only the last N entries (default: all).",
                },
            },
        },
    },
}

_ATTACH_FILE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "attach_file",
        "description": (
            "Attach a file to your response so the user can download it. "
            "The user cannot access agent file systems — this is the ONLY "
            "way to deliver a file to the user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "Filename to attach (e.g. 'report.md').",
                },
            },
            "required": ["file"],
        },
    },
}

_LIST_INBOX_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "list_inbox",
        "description": "List files in the inbox directory. Shows all available files that can be read, attached, or forwarded.",
        "parameters": {"type": "object", "properties": {}},
    },
}

_WRITE_FILE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write text content to a file in the inbox.",
        "parameters": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Filename (e.g. 'output.txt')."},
                "content": {"type": "string", "description": "Content to write."},
            },
            "required": ["file", "content"],
        },
    },
}

_DELETE_FILE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "delete_file",
        "description": "Delete a file from the inbox.",
        "parameters": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Filename to delete."},
            },
            "required": ["file"],
        },
    },
}

# ---------------------------------------------------------------------------
# Minimal web_fetch (regex-based HTML stripping, no MarkItDown)
# ---------------------------------------------------------------------------

_FETCH_USER_AGENT = "Mozilla/5.0 (compatible; BackplanedCore/1.0)"
_FETCH_MAX_CHARS = 12000


def _html_to_text(raw_html: str) -> str:
    """Regex-based HTML → plain text (lightweight, no external deps)."""
    text = re.sub(r"<script[\s\S]*?</script>", "", raw_html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<h([1-6])[^>]*>([\s\S]*?)</h\1>",
                  lambda m: f"\n{'#' * int(m[1])} {re.sub(r'<[^>]+>', '', m[2]).strip()}\n",
                  text, flags=re.I)
    text = re.sub(r"<li[^>]*>([\s\S]*?)</li>",
                  lambda m: f"\n- {re.sub(r'<[^>]+>', '', m[1]).strip()}", text, flags=re.I)
    text = re.sub(r"</(p|div|section|article)>", "\n\n", text, flags=re.I)
    text = re.sub(r"<(br|hr)\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def _web_fetch(url: str) -> str:
    """Fetch a URL and return extracted text content."""
    from urllib.parse import urlparse
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return f"Error: only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return "Error: missing domain"
    except Exception as e:
        return f"Error: invalid URL: {e}"

    try:
        async with httpx.AsyncClient(
            follow_redirects=True, max_redirects=5, timeout=15.0,
        ) as client:
            r = await client.get(url, headers={"User-Agent": _FETCH_USER_AGENT})
            r.raise_for_status()

        ctype = r.headers.get("content-type", "")
        if "application/json" in ctype:
            text = json.dumps(r.json(), indent=2, ensure_ascii=False)
        elif "text/html" in ctype or r.text[:256].lower().startswith(("<!doctype", "<html")):
            text = _html_to_text(r.text)
        else:
            text = r.text.strip()

        if len(text) > _FETCH_MAX_CHARS:
            text = text[:_FETCH_MAX_CHARS] + "\n\n[Content truncated]"

        return f"URL: {r.url}\n\n{text}"
    except Exception as e:
        return f"Error fetching {url}: {e}"


_WEB_FETCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": (
            "Fetch a webpage and extract readable text (truncated to ~12k chars). "
            "Use for reading a specific URL the user provided or referenced. "
            "For general research, prefer call_web_agent instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to fetch"},
            },
            "required": ["url"],
        },
    },
}

_LOCAL_TOOL_NAMES = {"read_image", "read_file_text", "show_tool_history",
                     "attach_file", "list_inbox", "write_file", "delete_file",
                     "web_fetch"}

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
_DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".doc", ".ppt"}



_INBOX_BASE = Path(_AGENT_DIR) / "data" / "inboxes"


def _session_inbox(session_id: str) -> Path:
    """Return the per-session inbox directory, creating it if needed."""
    p = _INBOX_BASE / session_id / "inbox"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _to_inbox_display(abs_path: str) -> str:
    """Extract just the filename for LLM display."""
    return Path(abs_path).name


def _resolve_inbox_file(filename: str, session_id: str) -> Optional[str]:
    """Resolve a filename to an absolute path inside the session inbox.
    Returns None if the path escapes the inbox directory."""
    inbox = _session_inbox(session_id)
    try:
        resolved = (inbox / filename).resolve()
        resolved.relative_to(inbox.resolve())
        return str(resolved)
    except (ValueError, OSError):
        return None


def _mime_from_path(file_path: str) -> str:
    """Guess MIME type from file extension."""
    mt, _ = mimetypes.guess_type(file_path)
    return mt or "application/octet-stream"


def _handle_read_image(arguments: dict[str, Any], session_id: str) -> str:
    """Handle the read_image local tool. Returns a marker string for image injection."""
    filename = arguments.get("file", "")
    if not filename:
        return "Error: file is required."
    file_path = _resolve_inbox_file(filename, session_id)
    if not file_path:
        return "Error: file not found in inbox."
    try:
        data = Path(file_path).read_bytes()
        mime = _mime_from_path(file_path)
        if not mime.startswith("image/"):
            mime = "image/jpeg"
        b64 = base64.b64encode(data).decode("ascii")
        return f"[IMAGE_BASE64:{mime}:{b64}]"
    except Exception as exc:
        return f"Error reading image: {exc}"


_FILE_READ_MAX_CHARS: int = 50000  # ~12k tokens, safe for most context windows


def _handle_read_file_text(arguments: dict[str, Any], session_id: str) -> str:
    """Handle the read_file_text local tool. Returns file content as string."""
    filename = arguments.get("file", "")
    if not filename:
        return "Error: file is required."
    resolved = _resolve_inbox_file(filename, session_id)
    if not resolved:
        return "Error: file not found in inbox."
    file_path = resolved

    # Reject binary document formats — suggest md_converter instead
    ext = Path(file_path).suffix.lower()
    if ext in _DOCUMENT_EXTENSIONS:
        return (
            f"Error: '{ext}' is a binary document format that cannot be read as text. "
            f"Use call_md_converter(file=\"{filename}\") to convert it to readable Markdown first."
        )

    try:
        data = Path(file_path).read_bytes()

        # Detect binary content (high ratio of non-text bytes)
        sample = data[:4096]
        non_text = sum(1 for b in sample if b < 8 or (14 <= b < 32 and b != 27))
        if len(sample) > 0 and non_text / len(sample) > 0.1:
            return (
                f"Error: file appears to be binary ({len(data)} bytes). "
                f"Use call_md_converter(file=\"{filename}\") to convert it to readable text."
            )

        text = data.decode("utf-8", errors="replace")
        if len(text) > _FILE_READ_MAX_CHARS:
            text = text[:_FILE_READ_MAX_CHARS] + f"\n\n[Content truncated at {_FILE_READ_MAX_CHARS} characters. Total: {len(data)} bytes]"
        return text
    except Exception as exc:
        return f"Error reading file: {exc}"


def _extract_thinking_summary(thinking_blocks: Optional[list[dict[str, Any]]]) -> Optional[str]:
    """Extract a brief summary from thinking_blocks for verbose progress events.

    Handles multiple provider formats:
    - Anthropic: {"type": "thinking", "thinking": "...", "text": "..."}
    - Generic:   {"type": "thinking", "text": "..."}
    - Gemini:    {"provider": "gemini", "raw_content": {"parts": [{"text": "...", "thought": true}, ...]}}
    """
    if not thinking_blocks:
        return None
    texts: list[str] = []
    for b in thinking_blocks:
        # Gemini format: extract text from thought-marked parts
        if b.get("provider") == "gemini" and b.get("raw_content"):
            for part in b["raw_content"].get("parts", []):
                if part.get("thought") and part.get("text"):
                    texts.append(part["text"].strip())
        else:
            # Anthropic / generic format
            t = b.get("text") or b.get("thinking") or ""
            if t.strip():
                texts.append(t.strip())
    thinking = "\n".join(texts)
    if not thinking:
        return None
    # Get the last non-empty line (most relevant decision), capped at 200 chars.
    lines = [l.strip() for l in thinking.split("\n") if l.strip()]
    if not lines:
        return None
    summary = lines[-1]
    if len(summary) > 200:
        summary = "… " + summary[-200:]
    return summary


async def _push_progress(task_id: str, event_type: str, content: str = "", metadata: Optional[dict] = None) -> None:
    """Push a progress event (best-effort, non-blocking)."""
    token = _get_auth_token()
    if not token:
        return
    await push_progress_direct(ROUTER_URL, token, task_id, event_type, content, metadata)


def _build_tools(
    available_destinations: dict[str, Any],
) -> list[dict]:
    """Build OpenAI-format tool list from available_destinations, replacing
    memory_agent with the hand-crafted search_memory tool."""
    filtered = {k: v for k, v in available_destinations.items() if k != MEMORY_AGENT_ID}
    tools = build_openai_tools(filtered)
    tools.append(_SEARCH_MEMORY_TOOL)
    tools.append(_SHOW_TOOL_HISTORY_TOOL)
    tools.append(_READ_IMAGE_TOOL)
    tools.append(_READ_FILE_TEXT_TOOL)
    tools.append(_ATTACH_FILE_TOOL)
    tools.append(_LIST_INBOX_TOOL)
    tools.append(_WRITE_FILE_TOOL)
    tools.append(_DELETE_FILE_TOOL)
    tools.append(_WEB_FETCH_TOOL)
    return tools


# ---------------------------------------------------------------------------
# Router HTTP spawn (for parallel tool-call dispatching)
# ---------------------------------------------------------------------------

async def _spawn_via_http(
    identifier: str,
    parent_task_id: str,
    dest: str,
    payload: dict[str, Any],
    pending: Optional[dict[str, asyncio.Future]] = None,
) -> None:
    """POST a spawn request directly to the router's /route endpoint.

    On HTTP failure, if *pending* is provided and contains a Future for
    *identifier*, the Future is resolved with a synthetic error result so
    callers wake up immediately instead of waiting for a full timeout.
    """
    body = build_spawn_request(
        agent_id=_OUR_AGENT_ID,
        identifier=identifier,
        parent_task_id=parent_task_id,
        destination_agent_id=dest,
        payload=payload,
    )
    token = _get_auth_token()
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(
                f"{ROUTER_URL.rstrip('/')}/route",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
        except Exception as exc:
            logger.warning("Failed to spawn tool call %s → %s: %s", identifier, dest, exc)
            if pending is not None:
                fut = pending.get(identifier)
                if fut and not fut.done():
                    fut.set_result({
                        "status_code": 502,
                        "payload": {"content": f"Spawn failed: {exc}", "error": str(exc)},
                    })


# ---------------------------------------------------------------------------
# Memory-add helper (fire-and-forget; not exposed as LLM tool)
# ---------------------------------------------------------------------------

def _bg_memory_add(
    user_message: str,
    assistant_reply: str,
    user_id: str,
    parent_task_id: str,
    user_timezone: str = "UTC",
) -> None:
    """Fire-and-forget per-turn memory ingestion.

    Builds a small transcript of only the current exchange (user message +
    assistant reply) with a timezone-resolved timestamp, then spawns
    memory_add as a fire-and-forget task.  Uses the ``_noreply_`` identifier
    prefix so the router records the result but does not attempt to deliver
    it back (the origin agent has already finished).
    """
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(user_timezone))
        tz_label = user_timezone
    except Exception:
        now = datetime.now(timezone.utc)
        tz_label = "UTC"
    timestamp = now.strftime(f"%Y-%m-%d %H:%M {tz_label}")
    transcript = (
        f"[Current time: {timestamp}]\n"
        f"[{user_id}] {user_message}\n"
        f"[AI agent] {assistant_reply}"
    )

    payload: dict[str, Any] = {
        "operation": "add", "content": transcript, "user_id": user_id,
    }
    if user_timezone and user_timezone != "UTC":
        payload["timezone"] = user_timezone

    async def _fire() -> None:
        try:
            await _spawn_via_http(
                identifier=f"_noreply_madd_{uuid.uuid4().hex[:8]}",
                parent_task_id=parent_task_id,
                dest=MEMORY_AGENT_ID,
                payload=payload,
            )
        except Exception as exc:
            logger.debug("Background memory_add spawn failed: %s", exc)

    asyncio.ensure_future(_fire())


# ---------------------------------------------------------------------------
# Append tool-turn messages to the LLM context (backend-aware)
# ---------------------------------------------------------------------------

_IMAGE_MARKER_PREFIX = "[IMAGE_BASE64:"


def _is_image_result(content: str) -> bool:
    return content.startswith(_IMAGE_MARKER_PREFIX)


def _parse_image_marker(content: str) -> tuple[str, str]:
    """Parse '[IMAGE_BASE64:mime:b64data]' → (mime, b64data)."""
    inner = content[len(_IMAGE_MARKER_PREFIX):-1]  # strip prefix and trailing ]
    mime, _, b64 = inner.partition(":")
    return mime, b64


def _append_tool_turn(
    messages: list[dict],
    llm_resp: _LLMResponse,
    tc_results: dict[str, str],  # tc.id → result content string
) -> None:
    """
    Append the assistant's tool-call message and all tool results to messages.

    Always uses OpenAI format — llm_agent handles provider-specific conversion
    internally.  When a tool result is an image (marked with IMAGE_BASE64),
    an inline base64 user message with the image is injected after the tool
    results.
    """
    pending_images: list[tuple[str, str]] = []

    assistant_msg: dict[str, Any] = {
        "role": "assistant",
        "content": llm_resp.content,
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments),
                },
            }
            for tc in llm_resp.tool_calls
        ],
    }
    if llm_resp.thinking_blocks:
        assistant_msg["thinking_blocks"] = llm_resp.thinking_blocks
    messages.append(assistant_msg)
    for tc in llm_resp.tool_calls:
        result = tc_results.get(tc.id, "Error: no result received.")
        if _is_image_result(result):
            mime, b64 = _parse_image_marker(result)
            pending_images.append((mime, b64))
            result = "Image loaded. It will appear in the next message for your analysis."
        messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": result,
        })

    if pending_images:
        content_blocks: list[dict[str, Any]] = [
            {"type": "text", "text": "Here is the image for your analysis:"},
        ]
        for mime, b64 in pending_images:
            content_blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
        messages.append({"role": "user", "content": content_blocks})


# ---------------------------------------------------------------------------
# Core agent loop
# ---------------------------------------------------------------------------

_AGENT_ORIGIN_SYSTEM_PROMPT = (
    "You are processing an incoming request from another agent in a multi-agent system. "
    "Handle the request using available tools as instructed. "
    "You do not have access to the user's conversation history.\n\n"
    "## Important\n"
    "- Your results are sent back to the requesting agent, NOT to the user.\n"
    "- The attach_file tool attaches files to your result for the requesting agent — "
    "it does NOT send files to the user.\n"
    "- To communicate with the user (send messages or files), use call_channel_agent."
)


async def _agent_loop(
    user_message: str,
    user_id: str,
    session_id: str,
    available_destinations: dict[str, Any],
    loop_state: _LoopState,
    files: Optional[list[dict[str, Any]]] = None,
    session_changed_note: str = "",
    is_agent_origin: bool = False,
) -> str | AgentOutput:
    """
    Run the LLM agent loop for one user message.

    Handles history overflow, tool-call spawning with parallel asyncio.Futures,
    and persistent session history updates.  When ``files`` are provided,
    augments the user message with attachment info and enables file tools.

    When ``is_agent_origin`` is True (message from another agent, not the
    user), session history is not loaded or saved during the loop.  A
    dedicated system prompt is used instead of the user's configured one.
    After completion, only a clean user-facing notification entry is
    appended to history.
    """
    if is_agent_origin:
        history: list[dict] = []
    else:
        history = _load_history(session_id)
    tools = _build_tools(available_destinations)
    user_json_cfg = _load_user_json_config(user_id)
    user_config_md = user_json_cfg.get("user_config_md") or ""
    user_model_id = user_json_cfg.get("model_id") or LLM_MODEL_ID
    user_history_limit = int(user_json_cfg.get("history_token_limit") or HISTORY_TOKEN_LIMIT)
    user_system_prompt = user_json_cfg.get("system_prompt") or SYSTEM_PROMPT
    user_timezone = user_json_cfg.get("timezone") or "UTC"
    running_loop = asyncio.get_running_loop()

    # --- History overflow: truncate and ingest old portion into memory ---
    if not is_agent_origin and _history_tokens(history) > user_history_limit:
        half = user_history_limit // 2
        # Find group boundaries.  A "group" starts at a standalone user
        # message (not a tool-result or image-injection user message) and
        # includes everything up to the next standalone user message.
        # We can only cut at group boundaries to avoid orphaning tool
        # messages that reference a removed assistant's tool_call_id.
        group_starts: list[int] = []
        for i, msg in enumerate(history):
            if msg.get("role") != "user":
                continue
            # A tool-result user message has list content or follows an
            # assistant message with tool_calls.
            if isinstance(msg.get("content"), list):
                continue
            if i > 0 and history[i - 1].get("role") == "tool":
                continue
            group_starts.append(i)

        cut = 0
        for gs in group_starts:
            if gs == 0:
                continue
            if _history_tokens(history[gs:]) <= half:
                cut = gs
                break
        if cut == 0:
            if group_starts and group_starts[-1] > 0:
                # No group boundary fits in half; use the latest boundary.
                cut = group_starts[-1]
            else:
                # No usable group boundaries (all-tool-call history or
                # only boundary is at index 0).  Find the nearest safe
                # cut point at or after the midpoint: skip backwards from
                # the midpoint to avoid landing inside a tool-call
                # sequence (assistant-with-tool_calls → tool → user chain).
                mid = max(len(history) // 2, 1)
                # Walk forward from mid to find a position that is not
                # a tool message or a tool-result user message — i.e. a
                # message whose removal does not orphan prior tool_call_ids.
                while mid < len(history):
                    role = history[mid].get("role")
                    if role == "user" and not isinstance(history[mid].get("content"), list):
                        if mid == 0 or history[mid - 1].get("role") != "tool":
                            break
                    if role not in ("tool",):
                        # assistant or plain user — safe to cut before
                        if role == "assistant" and history[mid].get("tool_calls"):
                            mid += 1
                            continue
                        break
                    mid += 1
                if mid > 0 and mid < len(history):
                    cut = mid
        if cut > 0:
            truncated, history = history[:cut], history[cut:]
            _save_history(session_id, history)

    # ProxyFileManager handles file path ↔ ProxyFile translation.
    # Files arrive as router-proxy; pfm.fetch() downloads to per-session inbox
    # and registers local_path → original ProxyFile for outbound reuse.
    inbox_dir = _session_inbox(session_id)
    pfm = ProxyFileManager(
        inbox_dir=inbox_dir,
        router_url=ROUTER_URL,
    )
    local_paths: list[str] = []
    if files:
        for f in files:
            try:
                lp = await pfm.fetch(f, session_id)
                local_paths.append(lp)
            except Exception as exc:
                logger.warning("Failed to fetch file %s: %s", f.get("path"), exc)

    # --- Augment user message with file info (show inbox-relative paths) ---
    augmented_message = user_message
    if local_paths:
        file_lines: list[str] = []
        for lp in local_paths:
            fname = Path(lp).name
            ext = Path(lp).suffix.lower()
            if ext in _IMAGE_EXTENSIONS:
                file_lines.append(f'  - Image: {fname}')
            elif ext in _DOCUMENT_EXTENSIONS:
                file_lines.append(f'  - Document: {fname}')
            else:
                file_lines.append(f'  - Text file: {fname}')
        augmented_message = (
            f"{user_message}\n\n"
            f"[Attached files:\n" + "\n".join(file_lines) + "\n]"
        )

    # --- Build initial LLM context ---
    if is_agent_origin:
        system_parts = [_AGENT_ORIGIN_SYSTEM_PROMPT]
    else:
        system_parts = [user_system_prompt]
    system_parts.append(f"## Current User\nuser_id: {user_id}\nsession_id: {session_id}\ntimezone: {user_timezone}")
    if not is_agent_origin and user_config_md:
        system_parts.append(f"## User Configuration\n{user_config_md}")
    if session_changed_note:
        system_parts.append(f"## Session Notice\n{session_changed_note}")

    llm_messages: list[dict] = [
        {"role": "system", "content": "\n\n".join(system_parts)}
    ]
    for entry in history:
        llm_messages.append({"role": entry["role"], "content": entry["content"]})

    # Inject current time into the user message (not saved to history).
    # This prevents time hallucination without breaking prompt caching
    # (system prompt stays static; user message is unique per turn anyway).
    from zoneinfo import ZoneInfo as _ZI
    try:
        _user_tz = _ZI(user_timezone)
    except Exception:
        _user_tz = _ZI("UTC")
    _now = datetime.now(_user_tz)
    time_prefix = f"[Current time: {_now.strftime('%Y-%m-%d %H:%M %Z')} ({_now.strftime('%A')})]\n"
    llm_messages.append({"role": "user", "content": time_prefix + augmented_message})

    # Tool-call turn counter (for tool history records)
    tool_turn = 0
    # Tool names used across all turns (for history summary)
    tools_used: list[str] = []
    # Files collected via attach_file tool, included in final result
    attached_files: list[dict[str, Any]] = []
    # File references from tool results (persisted in history for cross-invocation awareness)
    result_file_refs: list[str] = []

    # --- Agent loop ---
    while not loop_state.cancelled:
        if tool_turn >= MAX_AGENT_ITERATIONS:
            last_content = ""
            for msg in reversed(llm_messages):
                if msg.get("role") == "assistant" and msg.get("content"):
                    last_content = msg["content"]
                    break
            reply = last_content or "(Agent iteration limit reached.)"
            if not is_agent_origin:
                history.append({"role": "user", "content": user_message})
                history.append({"role": "assistant", "content": reply})
                _save_history(session_id, history)
            return AgentOutput(content=reply)

        llm_resp = await _llm_call(
            llm_messages, tools, loop_state,
            model_id=user_model_id,
        )
        if llm_resp.usage:
            loop_state.prompt_tokens += llm_resp.usage.get("prompt_tokens", 0)
            loop_state.completion_tokens += llm_resp.usage.get("completion_tokens", 0)

        # Extract thinking summary for verbose progress events.
        # Content is already clean (llm_agent strips <think> tags).
        thinking_summary = _extract_thinking_summary(llm_resp.thinking_blocks)

        if not llm_resp.tool_calls:
            # Final answer — push progress and persist to session history.
            reply = llm_resp.content or ""
            await _push_progress(loop_state.task_id, "chunk", reply)

            if is_agent_origin:
                # Agent-originated: extract user-facing content and append
                # a clean notification entry (not the raw system instruction).
                notification_content = user_message
                for _marker in ("Notification:\n", "Message to deliver:\n"):
                    if _marker in user_message:
                        notification_content = user_message.split(_marker, 1)[1].strip()
                        break
                real_history = _load_history(session_id)
                real_history.append({"role": "assistant", "content": f"🔔 {notification_content}"})
                _save_history(session_id, real_history)
            else:
                # User-originated: full history persistence + memory ingestion.
                history_reply = reply
                if tools_used:
                    unique_tools = list(dict.fromkeys(tools_used))
                    history_reply += f"\n[Used tools: {', '.join(unique_tools)}]"
                if result_file_refs:
                    history_reply += "\n[Files received during this turn:\n" + "\n".join(result_file_refs) + "\n]"
                history.append({"role": "user", "content": user_message})
                history.append({"role": "assistant", "content": history_reply})
                _save_history(session_id, history)
                _bg_memory_add(user_message, history_reply, user_id, loop_state.task_id, user_timezone=user_timezone)

            files_out = [ProxyFile(**pf) for pf in attached_files] if attached_files else None
            return AgentOutput(content=reply, files=files_out)

        # Push thinking + tool call progress events.
        # Thinking summary is sent separately; only attach LLM's explicit
        # text message (not thinking) to the first tool_call event.
        if thinking_summary:
            await _push_progress(loop_state.task_id, "thinking", thinking_summary)
        tool_call_context = (llm_resp.content or "").strip()
        for tc in llm_resp.tool_calls:
            tool_msg = f"{tool_call_context}\nCalling {tc.name}" if tool_call_context else f"Calling {tc.name}"
            await _push_progress(
                loop_state.task_id, "tool_call",
                tool_msg,
                {"tool": tc.name, "arguments": tc.arguments},
            )
            tool_call_context = ""  # Only attach context to the first tool call

        # --- Dispatch tool calls ---
        # Local tools (read_image, read_file_text) are handled in-process.
        # Remote tools are spawned via the router with asyncio.Futures.
        tc_id_to_ident: dict[str, str] = {}   # tc.id   → router identifier
        ident_to_tc_id: dict[str, str] = {}   # identifier → tc.id
        futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        local_results: dict[str, str] = {}    # tc.id → result (for local tools)

        for i, tc in enumerate(llm_resp.tool_calls):
            # --- Local tools (no router spawn needed) ---
            if tc.name in _LOCAL_TOOL_NAMES:
                try:
                    if tc.name == "list_inbox":
                        try:
                            entries: list[str] = []
                            for p in sorted(inbox_dir.rglob("*")):
                                if p.is_file():
                                    size = p.stat().st_size
                                    entries.append(f"  {p.name} ({size} bytes)")
                            local_results[tc.id] = "\n".join(entries) if entries else "(inbox is empty)"
                        except Exception as exc:
                            local_results[tc.id] = f"Error: {exc}"
                    elif tc.name == "read_image":
                        local_results[tc.id] = _handle_read_image(tc.arguments, session_id)
                    elif tc.name == "read_file_text":
                        local_results[tc.id] = _handle_read_file_text(tc.arguments, session_id)
                    elif tc.name == "show_tool_history":
                        entries = _load_tool_history(session_id)
                        filt = tc.arguments.get("tool_name")
                        if filt:
                            entries = [e for e in entries if e.get("tool") == filt]
                        last_n = tc.arguments.get("last_n")
                        if last_n and last_n > 0:
                            entries = entries[-last_n:]
                        local_results[tc.id] = json.dumps(entries, ensure_ascii=False, indent=2) if entries else "No tool calls recorded in this session yet."
                    elif tc.name == "attach_file":
                        fn = tc.arguments.get("file", "")
                        resolved_fp = _resolve_inbox_file(fn, session_id) if fn else None
                        if not resolved_fp or not Path(resolved_fp).is_file():
                            local_results[tc.id] = f"Error: file not found: {fn}"
                        else:
                            pf_dict = pfm.resolve(resolved_fp)
                            attached_files.append(pf_dict)
                            local_results[tc.id] = f"File attached: {fn}"
                    elif tc.name == "write_file":
                        fn = tc.arguments.get("file", "")
                        resolved_fp = _resolve_inbox_file(fn, session_id) if fn else None
                        if not resolved_fp:
                            local_results[tc.id] = f"Error: invalid filename: {fn}"
                        else:
                            content = tc.arguments.get("content", "")
                            Path(resolved_fp).parent.mkdir(parents=True, exist_ok=True)
                            Path(resolved_fp).write_text(content, encoding="utf-8")
                            local_results[tc.id] = f"File written: {fn}"
                    elif tc.name == "delete_file":
                        fn = tc.arguments.get("file", "")
                        resolved_fp = _resolve_inbox_file(fn, session_id) if fn else None
                        if not resolved_fp or not Path(resolved_fp).exists():
                            local_results[tc.id] = f"Error: file not found: {fn}"
                        else:
                            Path(resolved_fp).unlink()
                            local_results[tc.id] = f"Deleted: {fn}"
                    elif tc.name == "web_fetch":
                        local_results[tc.id] = await _web_fetch(
                            tc.arguments.get("url", ""),
                        )
                except Exception as exc:
                    local_results[tc.id] = f"Error: {exc}"
                continue

            # --- fetch_agent_documentation (local handler) ---
            if tc.name == "fetch_agent_documentation":
                target_id = tc.arguments.get("agent_id", "")
                local_results[tc.id] = await handle_fetch_agent_documentation(
                    target_id, available_destinations, ROUTER_URL,
                )
                continue

            # --- Remote tools (spawned via router) ---
            if i > 0:
                await asyncio.sleep(0.1)

            identifier = f"tc_{uuid.uuid4().hex[:12]}"
            fut: asyncio.Future[dict[str, Any]] = running_loop.create_future()
            loop_state.pending[identifier] = fut
            futures[identifier] = fut
            tc_id_to_ident[tc.id] = identifier
            ident_to_tc_id[identifier] = tc.id

            if tc.name == "search_memory":
                await _spawn_via_http(
                    identifier=identifier,
                    parent_task_id=loop_state.task_id,
                    dest=MEMORY_AGENT_ID,
                    payload={
                        "operation": "search",
                        "content": tc.arguments.get("query", ""),
                        "user_id": user_id,
                        "count": tc.arguments.get("count", 5),
                    },
                    pending=loop_state.pending,
                )
            else:
                dest = (
                    tc.name[len("call_"):] if tc.name.startswith("call_") else tc.name
                )
                rewritten = pfm.resolve_in_args(tc.arguments)
                # Authoritative session-context injection: user_id / session_id
                # / timezone come from the current session, overriding whatever
                # the LLM generated.  This prevents a hallucinated or missing
                # user_id from bypassing per-user ACL in downstream agents
                # (e.g. llm_agent's allowed_models check, web_agent /
                # memory_agent / kb_agent's per-user model mapping).
                dest_info = available_destinations.get(dest, {})
                dest_schema = dest_info.get("input_schema", "")
                if "user_id" in dest_schema:
                    rewritten["user_id"] = user_id
                if "session_id" in dest_schema:
                    rewritten["session_id"] = session_id
                if user_timezone and user_timezone != "UTC":
                    if "timezone" in dest_schema or "session_id" in dest_schema:
                        rewritten["timezone"] = user_timezone
                await _spawn_via_http(
                    identifier=identifier,
                    parent_task_id=loop_state.task_id,
                    dest=dest,
                    payload=rewritten,
                    pending=loop_state.pending,
                )

        # Wait for remote futures, with a per-batch deadline.
        if futures:
            done_set, pending_set = await asyncio.wait(
                set(futures.values()), timeout=TOOL_TIMEOUT
            )
            for fut in pending_set:
                fut.cancel()
        else:
            done_set, pending_set = set(), set()

        # Collect results and clean up pending registry.
        tc_results: dict[str, str] = dict(local_results)  # start with local results
        for ident, fut in futures.items():
            tc_id = ident_to_tc_id[ident]
            loop_state.pending.pop(ident, None)
            if fut in done_set and not fut.cancelled() and fut.exception() is None:
                result_data: dict[str, Any] = fut.result()
                content = await extract_result_text(
                    result_data, pfm, loop_state.task_id,
                    path_display_base=inbox_dir,
                )
                tc_results[tc_id] = content
                # Track file references for history persistence
                if "[Result files:" in content:
                    for line in content.split("\n"):
                        if "file:" in line and "- " in line:
                            result_file_refs.append(line.strip())
            else:
                exc_info = fut.exception() if fut in done_set and not fut.cancelled() else None
                logger.warning("Tool call %s timed out or failed: %s", ident, exc_info)
                tc_results[tc_id] = "Error: tool call failed or timed out."

        _append_tool_turn(llm_messages, llm_resp, tc_results)

        # Persist tool calls to session tool history.
        tool_turn += 1
        tools_used.extend(tc.name for tc in llm_resp.tool_calls)
        if not is_agent_origin:
            _append_tool_history(session_id, tool_turn, llm_resp.tool_calls, tc_results)

        # Push tool_result progress events.
        for tc in llm_resp.tool_calls:
            result_preview = tc_results.get(tc.id, "")[:200]
            await _push_progress(
                loop_state.task_id, "tool_result",
                f"Result from {tc.name}",
                {"tool": tc.name, "preview": result_preview},
            )
        # Loop continues — next iteration calls LLM with tool results in context.

    # Reached here if loop_state.cancelled was set.
    return AgentOutput(content="(Task cancelled by user.)")


# ---------------------------------------------------------------------------
# /config instruction handler (LLM-driven config modification)
# ---------------------------------------------------------------------------

_CONFIG_MODIFY_SYSTEM_PROMPT = """\
You are a configuration assistant. Your ONLY job is to modify user configuration \
based on the user's natural-language instruction.

You have one tool: `update_config`. Call it with a JSON object containing ONLY \
the fields you want to change.

## Fields

There are two distinct text fields — choose the right one:

- **user_config_md** (string): Markdown note describing the user's preferences, \
context, and how the assistant should behave. This is the PRIMARY field for \
personalisation. Most user instructions about tone, language, expertise, interests, \
or preferences should go here. Examples: "speak Korean", "I'm a data scientist", \
"always include code examples".

- **system_prompt** (string or null): The raw LLM system instruction that replaces \
the default prompt entirely. Only modify this when the user EXPLICITLY asks to \
change the "system prompt" itself. Setting to null resets to the built-in default.

Other fields:
- model_id (string or null): LLM model identifier
- summarization_model_id (string or null): Model used for summarization tasks \
(context consolidation, link/unlink briefs). Typically a faster/cheaper model. \
Falls back to model_id if null.
- history_token_limit (integer or null): max tokens kept in session history
- timezone (string or null): IANA timezone, e.g. "Asia/Seoul", "America/New_York"

## Rules
- Only change fields the user explicitly asks to change.
- When in doubt between user_config_md and system_prompt, prefer user_config_md.
- Set a field to null to reset it to the system default.
- Do NOT invent values — if the user's instruction is ambiguous, respond with \
a clarifying text message instead of calling the tool.
- After calling the tool, respond with a brief confirmation of what changed.
"""

_CONFIG_MODIFY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "update_config",
        "description": "Update user configuration fields. Only include fields to change.",
        "parameters": {
            "type": "object",
            "properties": {
                "model_id": {"type": ["string", "null"], "description": "LLM model identifier"},
                "summarization_model_id": {"type": ["string", "null"], "description": "Model for summarization tasks (context consolidation, link/unlink briefs). Falls back to model_id if null."},
                "history_token_limit": {"type": ["integer", "null"], "description": "Max tokens in session history"},
                "timezone": {"type": ["string", "null"], "description": "IANA timezone"},
                "system_prompt": {"type": ["string", "null"], "description": "Raw LLM system instruction (only modify if explicitly requested)"},
                "user_config_md": {"type": "string", "description": "Markdown user preferences and context (primary personalisation field)"},
            },
        },
    },
}

_USER_CONFIG_MD_EXAMPLE = """\
## User Preferences
- Preferred language: Korean
- Tone: casual and friendly

## Expertise
- Software engineer, 5 years experience
- Focus: backend development, Python

## Notes
- Prefers concise answers with code examples
- Interested in AI/ML topics"""


async def _handle_config_instruct(
    instruction: str,
    user_id: str,
    available_destinations: dict[str, Any],
    loop_state: _LoopState,
) -> str:
    """Run a one-shot LLM call to modify user config based on natural language."""
    cfg = _load_user_json_config(user_id)
    cfg_for_llm = {k: v for k, v in cfg.items() if k in _DEFAULT_USER_JSON_CONFIG}

    # When user_config_md is empty, include an example to guide the LLM
    example_note = ""
    if not cfg_for_llm.get("user_config_md"):
        example_note = f"\n\nExample user_config_md format (for reference):\n```markdown\n{_USER_CONFIG_MD_EXAMPLE}\n```"

    current_cfg_str = json.dumps(cfg_for_llm, indent=2, ensure_ascii=False)

    messages = [
        {"role": "system", "content": _CONFIG_MODIFY_SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"Current configuration:\n```json\n{current_cfg_str}\n```"
            f"{example_note}\n\n"
            f"User instruction: {instruction}"
        )},
    ]

    try:
        llm_resp = await _llm_call(
            messages, [_CONFIG_MODIFY_TOOL], loop_state,
            tool_choice={"type": "function", "function": {"name": "update_config"}},
        )
    except Exception as exc:
        return f"Config update failed: {exc}"

    # If LLM returned text without tool call (e.g. clarification)
    if not llm_resp.tool_calls:
        return llm_resp.content or "No changes made."

    # Apply the tool call
    tc = llm_resp.tool_calls[0]
    if tc.name != "update_config":
        return llm_resp.content or "No changes made."

    updates = tc.arguments
    for k in list(updates.keys()):
        if k not in _DEFAULT_USER_JSON_CONFIG:
            updates.pop(k)
    if not updates:
        return "No valid configuration changes requested."

    for k, v in updates.items():
        cfg[k] = v
    _save_user_json_config(user_id, cfg)

    # Build confirmation
    changed = ", ".join(f"{k}={v!r}" for k, v in updates.items())
    # If LLM also produced a text reply, use that
    reply = llm_resp.content or f"Configuration updated: {changed}"
    return reply


# ---------------------------------------------------------------------------
# /model handlers (list models, set model)
# ---------------------------------------------------------------------------

async def _handle_list_models(
    user_id: str,
    loop_state: _LoopState,
) -> str:
    """Query llm_agent for available models and format the result."""
    # Send <list_model_id> to llm_agent
    identifier = f"mdl_{uuid.uuid4().hex[:12]}"
    running_loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = running_loop.create_future()
    loop_state.pending[identifier] = fut

    await _spawn_via_http(
        identifier=identifier,
        parent_task_id=loop_state.task_id,
        dest=LLM_AGENT_ID,
        payload={
            "llmcall": {
                "messages": [{"role": "user", "content": "<list_model_id>"}],
                "tools": [],
            },
            "user_id": user_id,
        },
        pending=loop_state.pending,
    )

    try:
        result_data = await asyncio.wait_for(fut, timeout=TOOL_TIMEOUT)
    except asyncio.TimeoutError:
        return "Error: timed out querying available models."
    finally:
        loop_state.pending.pop(identifier, None)

    content = result_data.get("payload", {}).get("content", "")
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return f"Error: unexpected response from llm_agent: {content[:200]}"

    models = parsed.get("available_models", {})
    if not models:
        return "No models available."

    # Current user model
    cfg = _load_user_json_config(user_id)
    current = cfg.get("model_id") or LLM_MODEL_ID or "default"

    lines = ["**Available models:**", ""]
    for mid, info in models.items():
        marker = " ← current" if mid == current else ""
        lines.append(f"• `{mid}` — {info.get('provider', '?')}/{info.get('model', '?')}{marker}")
    lines.append("")
    lines.append("Usage: /model <model_id>")
    return "\n".join(lines)


async def _handle_set_model(
    target_model: str,
    user_id: str,
    loop_state: _LoopState,
) -> str:
    """Validate model_id via llm_agent ACL and update user config."""
    # Query llm_agent for allowed models
    identifier = f"mdl_{uuid.uuid4().hex[:12]}"
    running_loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = running_loop.create_future()
    loop_state.pending[identifier] = fut

    await _spawn_via_http(
        identifier=identifier,
        parent_task_id=loop_state.task_id,
        dest=LLM_AGENT_ID,
        payload={
            "llmcall": {
                "messages": [{"role": "user", "content": "<list_model_id>"}],
                "tools": [],
            },
            "user_id": user_id,
        },
        pending=loop_state.pending,
    )

    try:
        result_data = await asyncio.wait_for(fut, timeout=TOOL_TIMEOUT)
    except asyncio.TimeoutError:
        return "Error: timed out querying available models."
    finally:
        loop_state.pending.pop(identifier, None)

    content = result_data.get("payload", {}).get("content", "")
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return f"Error: unexpected response from llm_agent."

    allowed = parsed.get("available_models", {})

    if target_model not in allowed:
        available = ", ".join(f"`{m}`" for m in allowed)
        return f"Model `{target_model}` is not available for you.\nAvailable: {available}"

    cfg = _load_user_json_config(user_id)
    old_model = cfg.get("model_id") or LLM_MODEL_ID or "default"
    cfg["model_id"] = target_model
    _save_user_json_config(user_id, cfg)
    return f"Model changed: `{old_model}` → `{target_model}`"


# ---------------------------------------------------------------------------
# /link and /unlink handlers
# ---------------------------------------------------------------------------

_BRIEF_SYSTEM_PROMPT = """\
You are a conversation summarizer. Given a chat history, produce a concise but \
detail-preserving brief. Include: key topics discussed, decisions made, important \
facts mentioned, user preferences expressed, and any pending questions or tasks. \
Keep the brief under 500 words. Do NOT add commentary — output only the summary."""

_BRIEF_UPDATE_SYSTEM_PROMPT = """\
You are a conversation summarizer. You are given an existing brief and new \
conversation turns that happened after it. Produce an updated brief that \
incorporates the new information while staying concise and detail-preserving. \
Keep the result under 500 words. Do NOT add commentary — output only the \
updated summary."""


def _get_linkable_agents(available_destinations: dict[str, Any]) -> dict[str, dict]:
    """Return agents that accept LLMData input and are not hidden."""
    linkable: dict[str, dict] = {}
    for aid, info in available_destinations.items():
        schema = info.get("input_schema", "")
        if "LLMData" not in schema and "llmdata" not in schema.lower():
            continue
        if info.get("hidden"):
            continue
        # Exclude internal agents
        if aid in (LLM_AGENT_ID, MEMORY_AGENT_ID):
            continue
        linkable[aid] = info
    return linkable


async def _llm_brief(
    text: str,
    loop_state: _LoopState,
    system_prompt: str = _BRIEF_SYSTEM_PROMPT,
    model_id: Optional[str] = None,
) -> str:
    """One-shot LLM call to produce a brief summary. Returns the summary text."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text},
    ]
    try:
        resp = await _llm_call(messages, [], loop_state, model_id=model_id)
        return resp.content or ""
    except Exception as exc:
        logger.warning("Brief generation failed: %s", exc)
        return "(Brief generation failed.)"


async def _handle_list_linkable(
    available_destinations: dict[str, Any],
) -> str:
    """List agents available for direct-link mode."""
    linkable = _get_linkable_agents(available_destinations)
    if not linkable:
        return "No agents available for direct link."
    lines = ["**Agents available for direct link:**", ""]
    for aid, info in linkable.items():
        lines.append(f"• `{aid}` — {info.get('description', 'No description.')}")
    lines.append("")
    lines.append("Usage: /link <agent_id>")
    return "\n".join(lines)


async def _handle_link_agent(
    target_agent: str,
    user_id: str,
    session_id: str,
    available_destinations: dict[str, Any],
    loop_state: _LoopState,
) -> str:
    """Activate direct-link mode to the specified agent."""
    # Validate target
    linkable = _get_linkable_agents(available_destinations)
    if target_agent not in linkable:
        available = ", ".join(f"`{a}`" for a in linkable)
        return f"Agent `{target_agent}` is not available for direct link.\nAvailable: {available}"

    # Check if already linked
    existing = _load_link_state(session_id)
    if existing:
        return f"Already linked to `{existing['agent_id']}`. Use /unlink first."

    # Brief current session history
    history = _load_history(session_id)
    brief = ""
    if history:
        transcript = _history_to_transcript(history)
        brief = await _llm_brief(
            transcript, loop_state,
            model_id=_resolve_summarization_model(user_id),
        )

    # Create link state
    state: dict[str, Any] = {
        "agent_id": target_agent,
        "brief": brief,
        "linked_at": datetime.now(timezone.utc).isoformat(),
        "history_since_link": [],
    }
    _save_link_state(session_id, state)

    # Add activation note to main history
    activation_msg = f"(Link activated: direct communication with {target_agent})"
    history.append({"role": "assistant", "content": activation_msg})
    _save_history(session_id, history)

    desc = linkable[target_agent].get("description", "")
    return (
        f"Linked to **{target_agent}**.\n"
        f"_{desc}_\n\n"
        f"Messages will be sent directly to this agent. Use /unlink to return to normal mode."
    )


async def _handle_unlink_agent(
    session_id: str,
    loop_state: Optional[_LoopState] = None,
    user_id: Optional[str] = None,
) -> str:
    """Deactivate direct-link mode. Briefs linked conversation and appends summary to main history."""
    state = _load_link_state(session_id)
    if not state:
        return "No active link."

    agent_id = state.get("agent_id", "unknown")
    post_link = state.get("history_since_link", [])
    brief = state.get("brief", "")

    # Resolve summarization model for brief generation
    summ_model = _resolve_summarization_model(user_id) if user_id else None

    # Brief the linked conversation (combine existing brief with post-link history)
    link_summary = ""
    if post_link and loop_state:
        parts: list[str] = []
        if brief:
            parts.append(f"Context before link:\n{brief}")
        parts.append(f"Conversation with {agent_id}:\n{_history_to_transcript(post_link)}")
        link_summary = await _llm_brief("\n\n".join(parts), loop_state, model_id=summ_model)
    elif post_link:
        # No loop_state (called from /new) — use raw transcript as fallback
        link_summary = _history_to_transcript(post_link)

    # Replace raw linked exchanges in main history with a brief summary
    history = _load_history(session_id)
    # Remove linked exchanges that were appended during link mode
    # (everything after the activation note)
    activation_idx = None
    for i in range(len(history) - 1, -1, -1):
        c = history[i].get("content", "")
        if c.startswith("(Link activated:"):
            activation_idx = i
            break
    if activation_idx is not None:
        history = history[:activation_idx]  # trim everything from activation onward

    # Append a single summary entry
    summary_msg = f"(Direct chat with {agent_id} ended."
    if link_summary:
        summary_msg += f" Summary: {link_summary}"
    summary_msg += ")"
    history.append({"role": "assistant", "content": summary_msg})
    _save_history(session_id, history)

    _clear_link_state(session_id)
    return f"Unlinked from **{agent_id}**. Returning to normal mode."


# ---------------------------------------------------------------------------
# Linked-mode message handling
# ---------------------------------------------------------------------------

async def _truncate_link_history(
    session_id: str,
    state: dict[str, Any],
    user_history_limit: int,
    loop_state: _LoopState,
    user_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    If post-link history exceeds the linked-mode token limit, use LLM to
    update the brief with the old portion, then truncate keeping the newest
    fraction (LINK_TRUNCATION_KEEP_RATIO).
    """
    link_limit = int(user_history_limit * LINK_HISTORY_TOKEN_RATIO)
    post_link = state.get("history_since_link", [])

    if _history_tokens(post_link) <= link_limit:
        return state  # no truncation needed

    keep_tokens = int(link_limit * LINK_TRUNCATION_KEEP_RATIO)

    # Find cut point (similar to main history group boundary logic)
    cut = 0
    for i in range(len(post_link)):
        if post_link[i].get("role") == "user":
            if _history_tokens(post_link[i:]) <= keep_tokens:
                cut = i
                break
    if cut == 0:
        # Can't find good boundary, keep second half
        cut = len(post_link) // 2

    old_portion = post_link[:cut]
    kept_portion = post_link[cut:]

    # Update brief with old portion via LLM
    summ_model = _resolve_summarization_model(user_id) if user_id else None
    if old_portion:
        old_transcript = _history_to_transcript(old_portion)
        current_brief = state.get("brief", "")
        update_input = (
            f"Existing brief:\n{current_brief}\n\n"
            f"New conversation turns:\n{old_transcript}"
        )
        updated_brief = await _llm_brief(
            update_input, loop_state,
            system_prompt=_BRIEF_UPDATE_SYSTEM_PROMPT,
            model_id=summ_model,
        )
        state["brief"] = updated_brief

    state["history_since_link"] = kept_portion
    _save_link_state(session_id, state)
    return state


async def _handle_linked_message(
    message: str,
    user_id: str,
    session_id: str,
    loop_state: _LoopState,
    state: dict[str, Any],
    available_destinations: Optional[dict[str, Any]] = None,
    files: Optional[list[dict[str, Any]]] = None,
) -> str | AgentOutput:
    """Handle a user message in linked mode: spawn to linked agent, return result."""
    agent_id = state["agent_id"]
    user_json_cfg = _load_user_json_config(user_id)
    user_history_limit = int(user_json_cfg.get("history_token_limit") or HISTORY_TOKEN_LIMIT)

    # Truncate link history if needed (updates brief in place)
    state = await _truncate_link_history(session_id, state, user_history_limit, loop_state, user_id=user_id)

    # Build context from brief + post-link history
    context_parts: list[str] = []
    brief = state.get("brief", "")
    if brief:
        context_parts.append(f"## Session context (before link)\n{brief}")
    post_link = state.get("history_since_link", [])
    if post_link:
        transcript = _history_to_transcript(post_link)
        context_parts.append(f"## Conversation since link\n{transcript}")

    # Check if linked agent accepts files
    # (File-accepting agents have their own file info injection in their system prompt,
    # so we only pass the files payload — no need to augment the prompt text.)
    dest_info = (available_destinations or {}).get(agent_id, {})
    dest_schema = dest_info.get("input_schema", "")
    agent_accepts_files = "files" in dest_schema.lower() or "ProxyFile" in dest_schema

    # Build spawn payload
    spawn_payload: dict[str, Any] = {
        "llmdata": {
            "agent_instruction": f"You are in a direct conversation with user '{user_id}'.",
            "context": "\n\n".join(context_parts) if context_parts else None,
            "prompt": message,
        },
    }
    if files and agent_accepts_files:
        spawn_payload["files"] = files
    # Pass user_id if agent accepts it
    if "user_id" in dest_schema:
        spawn_payload["user_id"] = user_id
    # Pass session_id if agent accepts it (e.g. reminder_agent, cron_agent)
    if "session_id" in dest_schema:
        spawn_payload["session_id"] = session_id
    # Pass timezone if agent accepts it or uses session_id
    user_timezone = user_json_cfg.get("timezone") or "UTC"
    if user_timezone and user_timezone != "UTC":
        if "timezone" in dest_schema or "session_id" in dest_schema:
            spawn_payload["timezone"] = user_timezone

    # Spawn to linked agent
    identifier = f"link_{uuid.uuid4().hex[:12]}"
    running_loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = running_loop.create_future()
    loop_state.pending[identifier] = fut

    await _spawn_via_http(
        identifier=identifier,
        parent_task_id=loop_state.task_id,
        dest=agent_id,
        payload=spawn_payload,
        pending=loop_state.pending,
    )

    try:
        result_data = await asyncio.wait_for(fut, timeout=TOOL_TIMEOUT)
    except asyncio.TimeoutError:
        return f"(From: {agent_id}) Error: request timed out."
    finally:
        loop_state.pending.pop(identifier, None)

    result_payload = result_data.get("payload", {})
    content = result_payload.get("content", "")
    status_code = result_data.get("status_code", 200)
    if status_code and status_code >= 400:
        content = f"Error ({status_code}): {content}"

    reply = f"(From: {agent_id}) {content}"

    # Update link state only (NOT main history — briefed on unlink)
    post_link.append({"role": "user", "content": message})
    post_link.append({"role": "assistant", "content": reply})
    state["history_since_link"] = post_link
    _save_link_state(session_id, state)

    # Preserve any files the linked agent returned.
    result_files = result_payload.get("files")
    if result_files:
        return AgentOutput(
            content=reply,
            files=[ProxyFile(**f) if isinstance(f, dict) else f for f in result_files],
        )
    return reply


# ---------------------------------------------------------------------------
# Message dispatch (special tokens + normal agent loop)
# ---------------------------------------------------------------------------

async def _dispatch(
    message: str,
    user_id: str,
    session_id: str,
    available_destinations: dict[str, Any],
    loop_state: _LoopState,
    files: Optional[list[dict[str, Any]]] = None,
    session_changed_note: str = "",
    origin_agent_id: str = "",
) -> str | AgentOutput:
    """Route the incoming message to the appropriate handler."""

    if message.startswith("<new_session>"):
        # Format: "<new_session>" or "<new_session> {new_session_id}"
        parts = message.split(None, 1)
        new_sid = parts[1] if len(parts) > 1 else None
        # Unlink if active (briefs linked conversation into main history)
        if _load_link_state(session_id):
            await _handle_unlink_agent(session_id, loop_state, user_id=user_id)
        _archive_and_clear(session_id)
        if new_sid:
            _set_active_session(user_id, new_sid)
        return "Session archived."

    if message == "<token_info>":
        history = _load_history(session_id)
        tokens = _history_tokens(history)
        _ujc = _load_user_json_config(user_id)
        _limit = int(_ujc.get("history_token_limit") or HISTORY_TOKEN_LIMIT)
        return f"Estimated tokens in current session: {tokens} / {_limit}"

    if message == "<agents_info>":
        if not available_destinations:
            return "No available agents."
        return "\n".join(
            f"**{aid}**: {info.get('description', 'No description.')}"
            for aid, info in available_destinations.items()
        )

    if message.startswith("<user_config>"):
        cfg_text = message[len("<user_config>"):].strip()
        json_cfg = _load_user_json_config(user_id)
        json_cfg["user_config_md"] = cfg_text
        _save_user_json_config(user_id, json_cfg)
        return f"User configuration saved for {user_id}."

    if message == "<fetch_user_config>":
        cfg = _load_user_json_config(user_id)
        return json.dumps(cfg, indent=2, ensure_ascii=False)

    if message.startswith("<update_user_config>"):
        raw = message[len("<update_user_config>"):].strip()
        try:
            updates = json.loads(raw)
        except json.JSONDecodeError as e:
            return f"Error: invalid JSON — {e}"
        cfg = _load_user_json_config(user_id)
        for k in _DEFAULT_USER_JSON_CONFIG:
            if k in updates:
                cfg[k] = updates[k]
        _save_user_json_config(user_id, cfg)
        return json.dumps(cfg, indent=2, ensure_ascii=False)

    # --- /config (no args) → show current user config ---
    if message == "<show_config>":
        cfg = _load_user_json_config(user_id)
        lines = [f"**Configuration for {user_id}**", ""]
        lines.append(f"Model: {cfg.get('model_id') or LLM_MODEL_ID or 'default'}")
        summ_mid = cfg.get("summarization_model_id")
        lines.append(f"Summarization model: {summ_mid or '(uses main model)'}")
        lines.append(f"History token limit: {cfg.get('history_token_limit') or HISTORY_TOKEN_LIMIT}")
        lines.append(f"Timezone: {cfg.get('timezone') or 'UTC'}")
        sp = cfg.get("system_prompt")
        if sp:
            lines.append(f"System prompt:\n{sp}")
        else:
            lines.append("System prompt: (default)")
        ucmd = cfg.get("user_config_md")
        if ucmd:
            lines.append(f"User config note:\n{ucmd}")
        else:
            lines.append("User config note: (empty)")
        return "\n".join(lines)

    # --- /config <instruction> → LLM-driven config modification ---
    if message.startswith("<config_instruct>"):
        instruction = message[len("<config_instruct>"):].strip()
        if not instruction:
            return "Error: no instruction provided."
        return await _handle_config_instruct(
            instruction, user_id, available_destinations, loop_state,
        )

    # --- /model (no args) → list available models ---
    if message == "<list_models>":
        return await _handle_list_models(user_id, loop_state)

    # --- /model <model_id> → change user model ---
    if message.startswith("<set_model>"):
        target_model = message[len("<set_model>"):].strip()
        if not target_model:
            return "Error: no model_id provided."
        return await _handle_set_model(target_model, user_id, loop_state)

    # --- /link (no args) → list linkable agents ---
    if message == "<list_linkable>":
        return await _handle_list_linkable(available_destinations)

    # --- /link <agent_id> → activate direct link ---
    if message.startswith("<link_agent>"):
        target = message[len("<link_agent>"):].strip()
        if not target:
            return await _handle_list_linkable(available_destinations)
        return await _handle_link_agent(
            target, user_id, session_id, available_destinations, loop_state,
        )

    # --- /unlink → deactivate direct link ---
    if message == "<unlink_agent>":
        return await _handle_unlink_agent(session_id, loop_state, user_id=user_id)

    # --- Linked-mode intercept: bypass agent loop, route to linked agent ---
    # Only user-initiated messages (from channel_agent) respect linked mode.
    # Proactive messages from other agents (reminder_agent, cron_agent, etc.)
    # must go through the normal orchestrator loop so they can be delivered
    # to the user via channel_agent, not forwarded to the linked agent.
    link_state = _load_link_state(session_id)
    if link_state and origin_agent_id == "channel_agent":
        return await _handle_linked_message(
            message, user_id, session_id, loop_state, link_state,
            available_destinations=available_destinations,
            files=files,
        )

    # --- Non-user messages flag ---
    # Messages from other agents (reminder_agent, cron_agent, etc.) reuse
    # _agent_loop but skip session history loading/saving and use a
    # dedicated system prompt.  A clean notification entry is appended
    # to history afterward so the user can reference it.
    is_agent_origin = bool(origin_agent_id and origin_agent_id != "channel_agent")

    return await _agent_loop(
        user_message=message,
        user_id=user_id,
        session_id=session_id,
        available_destinations=available_destinations,
        loop_state=loop_state,
        files=files,
        session_changed_note=session_changed_note,
        is_agent_origin=is_agent_origin,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def _run(data: dict[str, Any]) -> dict[str, Any]:
    task_id: str = data.get("task_id", "")
    parent_task_id: Optional[str] = data.get("parent_task_id")
    origin_agent_id: str = str(data.get("agent_id") or "")
    payload: dict[str, Any] = data.get("payload", {})
    available_destinations: dict[str, Any] = data.get("available_destinations") or {}

    user_id = str(payload.get("user_id") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    message = str(payload.get("message") or "").strip()
    files: Optional[list[dict[str, Any]]] = payload.get("files")

    if not user_id or not session_id or not message:
        return build_result_request(
            agent_id=_OUR_AGENT_ID,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=400,
            output=AgentOutput(
                content="Error: user_id, session_id, and message are all required."
            ),
        )

    # Handle <stop_session> — cancel all active loops for this session.
    if message == "<stop_session>":
        cancelled_count = 0
        cancelled_tasks: list[tuple[str, Optional[str]]] = []
        for ls in list(_active_loops.values()):
            if ls.session_id == session_id and ls.user_id == user_id:
                ls.cancelled = True
                for fut in ls.pending.values():
                    if not fut.done():
                        fut.cancel()
                cancelled_tasks.append((ls.task_id, ls.parent_task_id))
                cancelled_count += 1
        # Report cancelled result for each active task so the router
        # moves them out of 'active' state.
        token = _get_auth_token()
        for ctid, cptid in cancelled_tasks:
            cancel_result = build_result_request(
                agent_id=_OUR_AGENT_ID,
                task_id=ctid,
                parent_task_id=cptid,
                status_code=499,
                output=AgentOutput(content="Task cancelled by user."),
            )
            try:
                async with httpx.AsyncClient(timeout=10.0) as c:
                    await c.post(
                        f"{ROUTER_URL}/route", json=cancel_result,
                        headers={"Authorization": f"Bearer {token}"},
                    )
            except Exception:
                pass
        return build_result_request(
            agent_id=_OUR_AGENT_ID,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=200,
            output=AgentOutput(
                content=f"Cancelled {cancelled_count} active task(s)." if cancelled_count else "No active tasks to cancel."
            ),
        )

    # Resolve session: redirect stale session_ids to the user's active session.
    # Control tokens (<new_session>, etc.) operate on the session_id as-given
    # and must NOT be redirected or prefixed.
    _CONTROL_PREFIXES = ("<new_session>", "<stop_session>",
                         "<token_info>", "<agents_info>",
                         "<user_config>", "<fetch_user_config>", "<update_user_config>",
                         "<show_config>", "<config_instruct>",
                         "<list_models>", "<set_model>",
                         "<list_linkable>", "<link_agent>", "<unlink_agent>")
    is_control = any(message.startswith(p) for p in _CONTROL_PREFIXES)

    session_changed_note = ""
    if not is_control:
        resolved_sid, original_sid = _resolve_session(user_id, session_id)
        if original_sid is not None:
            logger.info(
                "Session resolved: %s → %s (task %s)",
                original_sid, resolved_sid, task_id,
            )
            session_id = resolved_sid
            session_changed_note = (
                f"Important info to include in result report (not in direct "
                f"message to user): session_id has been changed from "
                f"{original_sid} to {resolved_sid}."
            )

    loop_state = _LoopState(
        task_id=task_id,
        parent_task_id=parent_task_id,
        session_id=session_id,
        user_id=user_id,
    )
    _active_loops[task_id] = loop_state

    try:
        content = await asyncio.wait_for(
            _dispatch(
                message=message,
                session_changed_note=session_changed_note,
                user_id=user_id,
                session_id=session_id,
                available_destinations=available_destinations,
                loop_state=loop_state,
                files=files,
                origin_agent_id=origin_agent_id,
            ),
            timeout=AGENT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("Agent loop timed out for task %s (session %s)", task_id, session_id)
        return build_result_request(
            agent_id=_OUR_AGENT_ID,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=504,
            output=AgentOutput(content="Request timed out."),
        )
    except Exception as exc:
        logger.exception("Unhandled error in agent loop for task %s: %s", task_id, exc)
        return build_result_request(
            agent_id=_OUR_AGENT_ID,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=500,
            output=AgentOutput(content=f"Agent error: {exc}"),
        )
    finally:
        _active_loops.pop(task_id, None)

    if isinstance(content, AgentOutput):
        output = content
    else:
        output = AgentOutput(content=content)
    return build_result_request(
        agent_id=_OUR_AGENT_ID,
        task_id=task_id,
        parent_task_id=parent_task_id,
        status_code=200,
        output=output,
    )


# ---------------------------------------------------------------------------
# FastAPI app (required by embedded agent loader)
# ---------------------------------------------------------------------------

app = FastAPI(title="Core Personal Agent")


@app.post("/receive")
async def receive(request: Request) -> JSONResponse:
    """
    Called by the router via in-process ASGI transport.

    Two cases:
    1. Result delivery (identifier set, destination_agent_id absent, status_code
       present) — resolves the waiting asyncio.Future and returns JSON null so
       the router skips _process_route_internal.
    2. New agent invocation — runs the full agent loop and returns a result
       routing payload.
    """
    _refresh_config()
    data = await request.json()

    # --- Case 1: result delivery for a pending tool call ---
    identifier: Optional[str] = data.get("identifier")
    destination: Optional[str] = data.get("destination_agent_id")
    if identifier and destination is None and "status_code" in data:
        for loop_state in _active_loops.values():
            fut = loop_state.pending.get(identifier)
            if fut is not None and not fut.done():
                fut.set_result(data)
                break
        # Return null — the router's isinstance(response_data, dict) check
        # prevents _process_route_internal from being called.
        return JSONResponse(status_code=200, content=None)

    # --- Case 2: new agent invocation ---
    try:
        result = await _run(data)
        return JSONResponse(status_code=200, content=result)
    except Exception as exc:
        task_id = data.get("task_id", "")
        parent_task_id = data.get("parent_task_id")
        return JSONResponse(
            status_code=200,
            content=build_result_request(
                agent_id=_OUR_AGENT_ID,
                task_id=task_id,
                parent_task_id=parent_task_id,
                status_code=500,
                output=AgentOutput(content=f"Fatal agent error: {exc}"),
            ),
        )
