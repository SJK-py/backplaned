"""
agents/web_agent/agent.py — LLM-backed web research embedded agent.

Accepts an LLMData prompt, autonomously searches the web and fetches
pages using its own tool loop, then synthesizes a report.

Embedded agent — loaded in-process by the router via ASGI transport.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

_ROOT = Path(__file__).resolve().parent.parent.parent
_AGENT_DIR = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from helper import (
    AgentInfo,
    AgentOutput,
    LLMData,
    build_result_request,
    build_spawn_request,
)

from tools import WEB_TOOLS, web_search, web_fetch

logger = logging.getLogger("web_agent")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CONFIG_PATH = _AGENT_DIR / "config.json"
_OUR_AGENT_ID = "web_agent"


def _load_config() -> dict[str, Any]:
    """Read config.json from disk (re-read on every call for hot-reload)."""
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cfg(key: str, default: str = "") -> str:
    """Get a config value as string, with fallback default."""
    return str(_load_config().get(key, default))


LLM_AGENT_ID: str = "llm_agent"
LLM_MODEL_ID: str = ""
SEARCH_PROVIDER: str = "searxng"
SEARXNG_BASE_URL: str = "http://localhost:8080"
BRAVE_API_KEY: str = ""
SEARCH_MAX_RESULTS: int = 5
FETCH_MAX_CHARS: int = 12000
FETCH_TIMEOUT: float = 15.0
AGENT_TIMEOUT: float = 120.0
ROUTER_URL: str = os.environ.get("ROUTER_URL", "http://localhost:8000")
_MAX_ITERATIONS: int = 10
_MAX_TOOL_CALLS: int = 15


def _refresh_config() -> None:
    """Re-read config.json and update module-level variables."""
    global LLM_AGENT_ID, LLM_MODEL_ID, SEARCH_PROVIDER, SEARXNG_BASE_URL
    global BRAVE_API_KEY, SEARCH_MAX_RESULTS, FETCH_MAX_CHARS, FETCH_TIMEOUT
    global AGENT_TIMEOUT, _MAX_ITERATIONS, _MAX_TOOL_CALLS
    cfg = _load_config()
    LLM_AGENT_ID = str(cfg.get("LLM_AGENT_ID", "llm_agent"))
    LLM_MODEL_ID = str(cfg.get("LLM_MODEL_ID", "")) or ""
    SEARCH_PROVIDER = str(cfg.get("SEARCH_PROVIDER", "searxng"))
    SEARXNG_BASE_URL = str(cfg.get("SEARXNG_BASE_URL", "http://localhost:8080"))
    BRAVE_API_KEY = str(cfg.get("BRAVE_API_KEY", ""))
    SEARCH_MAX_RESULTS = int(cfg.get("SEARCH_MAX_RESULTS", 5))
    FETCH_MAX_CHARS = int(cfg.get("FETCH_MAX_CHARS", 12000))
    FETCH_TIMEOUT = float(cfg.get("FETCH_TIMEOUT", 15))
    AGENT_TIMEOUT = float(cfg.get("AGENT_TIMEOUT", 120))
    _MAX_ITERATIONS = int(cfg.get("MAX_ITERATIONS", 10))
    _MAX_TOOL_CALLS = int(cfg.get("MAX_TOOL_CALLS", 15))


_refresh_config()  # Initial load

# ---------------------------------------------------------------------------
# AgentInfo
# ---------------------------------------------------------------------------

AGENT_INFO = AgentInfo(
    agent_id=_OUR_AGENT_ID,
    description=(
        "Web research agent. Searches the web and reads pages to produce a "
        "sourced report. Provide your research question in llmdata.prompt, "
        "and background context in llmdata.context if available."
    ),
    input_schema="llmdata: LLMData",
    output_schema="content: str",
    required_input=["llmdata"],
)

SYSTEM_PROMPT = """\
You are a web research agent in a multi-agent system. You search the web, \
read pages, and produce well-sourced reports for the calling agent.

## Process
1. Analyze the request to determine what to search.
2. Use web_search with ONE specific, targeted query.
3. Review search results. Fetch ONLY the 1-2 most relevant URLs.
4. If the first search answers the question, STOP. Do NOT search more.
5. Only search again if the first results are clearly insufficient.

## Limits
- Maximum 2 searches per request unless explicitly asked for more.
- Maximum 3 page fetches total. Each fetch returns truncated content — \
read what you get and work with it.
- Do NOT fetch URLs that are likely login walls, app stores, or aggregators.

## Result format
- Your result goes to another agent, not a user. Include enough detail \
and context for the caller to use your findings.
- Lead with key findings, then supporting details.
- Cite every claim: [Source Title](url)
- Note gaps or conflicts. Do NOT fabricate.
"""

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
# LLM call via llm_agent (spawn through router, wait for result)
# ---------------------------------------------------------------------------

# In-flight futures for LLM call results.
_pending: dict[str, asyncio.Future] = {}


async def _spawn_to_llm(
    identifier: str,
    payload: dict[str, Any],
    parent_task_id: Optional[str] = None,
) -> None:
    """POST a spawn request to the router targeting llm_agent.

    On failure, resolves the pending Future with a synthetic error so the
    caller wakes up immediately instead of waiting for a full timeout.
    """
    body = build_spawn_request(
        agent_id=_OUR_AGENT_ID,
        identifier=identifier,
        parent_task_id=parent_task_id,
        destination_agent_id=LLM_AGENT_ID,
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
            logger.warning("Failed to spawn to llm_agent: %s", exc)
            fut = _pending.get(identifier)
            if fut and not fut.done():
                fut.set_result({
                    "status_code": 502,
                    "payload": {"content": f"Spawn failed: {exc}", "error": str(exc)},
                })


async def _llm_call(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model_id: Optional[str] = None,
    tool_choice: Optional[Any] = None,
    parent_task_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> dict[str, Any]:
    """Call llm_agent and return the normalized response."""
    identifier = f"llm_{uuid.uuid4().hex[:12]}"
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = loop.create_future()
    _pending[identifier] = fut

    llmcall: dict[str, Any] = {
        "messages": messages,
        "tools": tools,
        "model_id": model_id or LLM_MODEL_ID or None,
    }
    if tool_choice is not None:
        llmcall["tool_choice"] = tool_choice

    payload: dict[str, Any] = {"llmcall": llmcall}
    if user_id:
        payload["user_id"] = user_id

    await _spawn_to_llm(identifier, payload, parent_task_id=parent_task_id)

    try:
        result_data = await asyncio.wait_for(fut, timeout=AGENT_TIMEOUT)
    except asyncio.TimeoutError:
        raise RuntimeError("LLM call timed out")
    finally:
        _pending.pop(identifier, None)

    raw = result_data.get("payload", {})
    sc = result_data.get("status_code", 200)
    content_str = raw.get("content", "")

    if sc and sc >= 400:
        raise RuntimeError(f"llm_agent error ({sc}): {content_str}")

    try:
        return json.loads(content_str)
    except (json.JSONDecodeError, TypeError):
        return {"content": content_str, "tool_calls": []}


# ---------------------------------------------------------------------------
# Local tool execution
# ---------------------------------------------------------------------------


async def _execute_tool(name: str, args: dict[str, Any]) -> str:
    """Execute a web tool locally and return the result string."""
    if name == "web_search":
        return await web_search(
            query=args.get("query", ""),
            provider=SEARCH_PROVIDER,
            searxng_base_url=SEARXNG_BASE_URL,
            brave_api_key=BRAVE_API_KEY,
            count=args.get("count", SEARCH_MAX_RESULTS),
            timeout=FETCH_TIMEOUT,
        )
    if name == "web_fetch":
        return await web_fetch(
            url=args.get("url", ""),
            max_chars=FETCH_MAX_CHARS,
            timeout=FETCH_TIMEOUT,
        )
    return f"Error: unknown tool '{name}'"


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


async def _run(data: dict[str, Any]) -> dict[str, Any]:
    """Process an inbound routing payload and return a result."""
    task_id: str = data.get("task_id", "")
    parent_task_id: Optional[str] = data.get("parent_task_id")
    raw_payload: dict[str, Any] = data.get("payload", {})
    user_id: Optional[str] = raw_payload.get("user_id")

    # Parse LLMData
    llmdata_raw = raw_payload.get("llmdata")
    if not llmdata_raw or not llmdata_raw.get("prompt"):
        return build_result_request(
            agent_id=_OUR_AGENT_ID,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=400,
            output=AgentOutput(content="Error: payload.llmdata.prompt is required"),
        )

    llmdata = LLMData.model_validate(llmdata_raw)

    # Build system prompt
    system_parts = [SYSTEM_PROMPT]
    if llmdata.agent_instruction:
        system_parts.append(f"## Additional Instructions\n{llmdata.agent_instruction}")
    if llmdata.context:
        system_parts.append(f"## Context\n{llmdata.context}")

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "\n\n".join(system_parts)},
        {"role": "user", "content": llmdata.prompt},
    ]

    # Agent loop: LLM decides what to search/fetch, we execute tools locally
    iteration = 0
    total_tool_calls = 0
    prompt_tokens = 0
    completion_tokens = 0
    llm_content: Optional[str] = None

    while iteration < _MAX_ITERATIONS:
        iteration += 1

        try:
            llm_result = await _llm_call(messages, WEB_TOOLS, parent_task_id=task_id, user_id=user_id)
        except RuntimeError as exc:
            logger.error("LLM call failed at iteration %d: %s", iteration, exc)
            return build_result_request(
                agent_id=_OUR_AGENT_ID,
                task_id=task_id,
                parent_task_id=parent_task_id,
                status_code=504,
                output=AgentOutput(content=f"LLM call failed: {exc}"),
            )

        llm_content = llm_result.get("content")
        llm_tool_calls = llm_result.get("tool_calls", [])
        llm_usage = llm_result.get("usage")
        if llm_usage:
            prompt_tokens += llm_usage.get("prompt_tokens", 0)
            completion_tokens += llm_usage.get("completion_tokens", 0)

        # Build assistant message for history
        assistant_dict: dict[str, Any] = {"role": "assistant"}
        if llm_content:
            assistant_dict["content"] = llm_content
        if llm_tool_calls:
            assistant_dict["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"]),
                    },
                }
                for tc in llm_tool_calls
            ]
        messages.append(assistant_dict)

        # No tool calls — final report
        if not llm_tool_calls:
            return build_result_request(
                agent_id=_OUR_AGENT_ID,
                task_id=task_id,
                parent_task_id=parent_task_id,
                status_code=200,
                output=AgentOutput(content=llm_content or "(No output)"),
            )

        # Execute tools locally
        for tc in llm_tool_calls:
            total_tool_calls += 1
            if total_tool_calls > _MAX_TOOL_CALLS:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": "Error: tool call limit reached. Write your final report now.",
                })
                continue

            result = await _execute_tool(tc["name"], tc.get("arguments", {}))
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

        # Warn LLM when running low on iterations
        remaining = _MAX_ITERATIONS - iteration
        if remaining <= 2:
            messages.append({
                "role": "user",
                "content": f"[System notice] Warning: {remaining} iteration(s) remaining. Write your final report now.",
            })

    # Max iterations — return whatever we have
    return build_result_request(
        agent_id=_OUR_AGENT_ID,
        task_id=task_id,
        parent_task_id=parent_task_id,
        status_code=200,
        output=AgentOutput(content=llm_content or "Max iterations reached."),
    )


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Web Agent")


@app.post("/receive")
async def receive(request: Request) -> JSONResponse:
    """
    Called by the router via in-process ASGI transport.

    Two cases:
    1. Result delivery from llm_agent — resolve pending future.
    2. New task — run the agent loop.
    """
    _refresh_config()
    data = await request.json()

    # Case 1: result delivery
    identifier: Optional[str] = data.get("identifier")
    dest: Optional[str] = data.get("destination_agent_id")
    if identifier and dest is None and "status_code" in data:
        fut = _pending.get(identifier)
        if fut is not None and not fut.done():
            fut.set_result(data)
        return JSONResponse(status_code=200, content=None)

    # Case 2: new task
    try:
        result = await _run(data)
        return JSONResponse(status_code=200, content=result)
    except Exception as exc:
        logger.exception("Unhandled error in web_agent")
        task_id = data.get("task_id", "")
        parent_task_id = data.get("parent_task_id")
        return JSONResponse(
            status_code=200,
            content=build_result_request(
                agent_id=_OUR_AGENT_ID,
                task_id=task_id,
                parent_task_id=parent_task_id,
                status_code=500,
                output=AgentOutput(content=f"Web agent error: {exc}"),
            ),
        )
