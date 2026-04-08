"""
agents/memory_agent/agent.py — LanceDB-backed long-term memory agent (embedded).

Supports two operations dispatched via the 'operation' field in payload:
  - add:    Ingest content into long-term memory for a user.
  - search: Retrieve the most relevant memories for a user given a query.

Loaded in-process by the router via ASGI transport.

LLM calls are routed through llm_agent for per-user model control.
Embedding calls are made directly to the configured endpoint (latency-
sensitive).  See memory_store.py for the two-pass memory algorithm.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import sys
import uuid
from functools import partial
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# Allow importing helper.py from the project root.
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from helper import AgentInfo, AgentOutput, build_result_request, build_spawn_request

# ---------------------------------------------------------------------------
# Configuration — read directly from config.json (hot-reloadable)
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).resolve().parent / "data" / "config.json"


def _load_config() -> dict[str, Any]:
    """Read config.json from disk (re-read on every call for hot-reload)."""
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cfg(key: str, default: str = "") -> str:
    """Get a config value as string, with fallback default."""
    val = str(_load_config().get(key, default))
    return val if val else default


# Connection params — read once at singleton init (not hot-reloadable).
LLM_AGENT_ID: str = _cfg("LLM_AGENT_ID", "llm_agent")
LLM_MODEL_ID: str = _cfg("LLM_MODEL_ID", "")

EMBED_BASE_URL: str = _cfg("EMBED_BASE_URL", "http://172.23.90.91:8000/api/v1")
EMBED_API_KEY: str = _cfg("EMBED_API_KEY", "placeholder")
EMBED_MODEL: str = _cfg("EMBED_MODEL", "Qwen3-Embedding-4B-GGUF")

EMBEDDING_DIMS: int = int(_cfg("EMBEDDING_DIMS", "768"))

DEFAULT_SEARCH_COUNT: int = int(_cfg("DEFAULT_SEARCH_COUNT", "5"))

ROUTER_URL: str = os.environ.get("ROUTER_URL", "http://localhost:8000")
LLM_CALL_TIMEOUT: float = 180.0


def _refresh_config() -> None:
    """Re-read config.json and update hot-reloadable variables."""
    global DEFAULT_SEARCH_COUNT
    cfg = _load_config()
    DEFAULT_SEARCH_COUNT = int(cfg.get("DEFAULT_SEARCH_COUNT", 5))


def _resolve_model_id(user_id: str) -> Optional[str]:
    """Resolve model_id for a user: per-user mapping > global default > None."""
    cfg = _load_config()
    user_models = cfg.get("USER_MODEL_IDS") or {}
    return user_models.get(user_id) or LLM_MODEL_ID or None


# ---------------------------------------------------------------------------
# AgentInfo — published to the router on registration
# ---------------------------------------------------------------------------

_OUR_AGENT_ID = "memory_agent"

AGENT_INFO = AgentInfo(
    agent_id=_OUR_AGENT_ID,
    description=(
        "Long-term memory store. operation='add': store content for a user. "
        "operation='search': retrieve relevant memories (returns JSON array). "
        "count sets max results for search (default 5)."
    ),
    input_schema="operation: str, content: str, user_id: str, count: Optional[int], timezone: Optional[str]",
    output_schema="content: str",
    required_input=["operation", "content", "user_id"],
)

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

_pending: dict[str, asyncio.Future] = {}


async def _spawn_to_llm(
    identifier: str,
    payload: dict[str, Any],
    parent_task_id: Optional[str] = None,
) -> None:
    """Send a spawn request to the router targeting llm_agent."""
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
                    "payload": {"content": f"Spawn failed: {exc}"},
                })


async def _llm_call_async(
    system: str,
    user_msg: str,
    *,
    user_id: Optional[str] = None,
    parent_task_id: Optional[str] = None,
) -> str:
    """Call llm_agent with a system+user message pair and return raw text."""
    identifier = f"mem_llm_{uuid.uuid4().hex[:12]}"
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = loop.create_future()
    _pending[identifier] = fut

    resolved_model = _resolve_model_id(user_id or "")
    llmcall: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        "tools": [],
    }
    if resolved_model:
        llmcall["model_id"] = resolved_model

    payload: dict[str, Any] = {"llmcall": llmcall}
    if user_id:
        payload["user_id"] = user_id

    await _spawn_to_llm(identifier, payload, parent_task_id=parent_task_id)

    try:
        result_data = await asyncio.wait_for(fut, timeout=LLM_CALL_TIMEOUT)
    except asyncio.TimeoutError:
        raise RuntimeError("LLM call timed out")
    finally:
        _pending.pop(identifier, None)

    raw = result_data.get("payload", {})
    sc = result_data.get("status_code", 200)
    content_str = raw.get("content", "")

    if sc and sc >= 400:
        raise RuntimeError(f"llm_agent error ({sc}): {content_str}")

    # llm_agent returns JSON with "content" key
    try:
        parsed = json.loads(content_str)
        return parsed.get("content", content_str)
    except (json.JSONDecodeError, TypeError):
        return content_str


def _make_sync_llm_fn(
    loop: asyncio.AbstractEventLoop,
    user_id: Optional[str] = None,
    parent_task_id: Optional[str] = None,
) -> Any:
    """Create a synchronous (system, user) -> str callable for MemoryStore.

    Bridges async llm_agent calls from within a thread-pool executor by
    scheduling coroutines on the main event loop.
    """
    def llm_fn(system: str, user_msg: str) -> str:
        coro = _llm_call_async(
            system, user_msg,
            user_id=user_id,
            parent_task_id=parent_task_id,
        )
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=LLM_CALL_TIMEOUT)
    return llm_fn


# ---------------------------------------------------------------------------
# MemoryStore singleton — lazily initialised on first use
# ---------------------------------------------------------------------------

_AGENT_DATA_DIR = Path(__file__).resolve().parent / "data"
_AGENT_DATA_DIR.mkdir(parents=True, exist_ok=True)

_store = None
_store_init_done: bool = False
_store_init_lock = threading.Lock()


def _get_store(llm_fn: Any) -> Any:
    """Return the MemoryStore singleton, creating it on first call."""
    global _store, _store_init_done
    if _store_init_done:
        # Swap in the current request's llm_fn (carries correct user_id)
        _store._llm_fn = llm_fn
        return _store
    with _store_init_lock:
        if not _store_init_done:
            # Dynamic import (router loads agent.py via importlib)
            import importlib.util as _ilu
            _ms_path = str(Path(__file__).resolve().parent / "memory_store.py")
            _ms_spec = _ilu.spec_from_file_location("memory_store", _ms_path)
            _ms_mod = _ilu.module_from_spec(_ms_spec)  # type: ignore
            _ms_spec.loader.exec_module(_ms_mod)  # type: ignore
            MemoryStore = _ms_mod.MemoryStore

            _store = MemoryStore(
                db_base=_AGENT_DATA_DIR / "lancedb",
                llm_fn=llm_fn,
                embed_base_url=EMBED_BASE_URL,
                embed_api_key=EMBED_API_KEY,
                embed_model=EMBED_MODEL,
                embedding_dims=EMBEDDING_DIMS,
            )
            _store_init_done = True
            logger.info(
                "MemoryStore initialised (db_base=%s, dims=%d)",
                _AGENT_DATA_DIR / "lancedb",
                EMBEDDING_DIMS,
            )
    _store._llm_fn = llm_fn
    return _store


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


async def _run(data: dict[str, Any]) -> dict[str, Any]:
    """Process an inbound routing payload and return a build_result_request dict."""
    task_id: str = data.get("task_id", "")
    parent_task_id: Optional[str] = data.get("parent_task_id")
    payload: dict[str, Any] = data.get("payload", {})

    operation: str = str(payload.get("operation") or "").strip().lower()
    content: str = str(payload.get("content") or "").strip()
    user_id: str = str(payload.get("user_id") or "").strip()
    count: int = min(int(payload.get("count") or DEFAULT_SEARCH_COUNT), 20)

    # --- Validate ---
    missing = [f for f, v in [("operation", operation), ("content", content), ("user_id", user_id)] if not v]
    if missing:
        return build_result_request(
            agent_id=_OUR_AGENT_ID,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=400,
            output=AgentOutput(content=f"Error: missing required field(s): {', '.join(missing)}"),
        )

    if operation not in ("add", "search"):
        return build_result_request(
            agent_id=_OUR_AGENT_ID,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=400,
            output=AgentOutput(content=f"Error: operation must be 'add' or 'search', got '{operation}'."),
        )

    loop = asyncio.get_running_loop()

    # Build a sync LLM callable that routes through llm_agent with the
    # correct user_id (for per-user model ACL).
    llm_fn = _make_sync_llm_fn(loop, user_id=user_id, parent_task_id=parent_task_id)
    store = _get_store(llm_fn)

    # --- Add ---
    if operation == "add":
        try:
            result = await loop.run_in_executor(
                None, partial(store.add, content, user_id=user_id)
            )
        except Exception as exc:
            logger.error("Memory add failed for user %s: %s", user_id, exc)
            return build_result_request(
                agent_id=_OUR_AGENT_ID,
                task_id=task_id,
                parent_task_id=parent_task_id,
                status_code=500,
                output=AgentOutput(content=f"Memory add failed: {exc}"),
            )
        return build_result_request(
            agent_id=_OUR_AGENT_ID,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=200,
            output=AgentOutput(content="Memory added successfully."),
        )

    # --- Search ---
    try:
        results = await loop.run_in_executor(
            None, partial(store.search, content, user_id=user_id, limit=count)
        )
    except Exception as exc:
        logger.error("Memory search failed for user %s: %s", user_id, exc)
        return build_result_request(
            agent_id=_OUR_AGENT_ID,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=500,
            output=AgentOutput(content=f"Memory search failed: {exc}"),
        )
    return build_result_request(
        agent_id=_OUR_AGENT_ID,
        task_id=task_id,
        parent_task_id=parent_task_id,
        status_code=200,
        output=AgentOutput(content=json.dumps(results, ensure_ascii=False)),
    )


# ---------------------------------------------------------------------------
# FastAPI app (required by embedded agent loader)
# ---------------------------------------------------------------------------

app = FastAPI(title="Memory Agent")


@app.post("/receive")
async def receive(request: Request) -> JSONResponse:
    """
    Called by the router via in-process ASGI transport.

    Two cases:
    1. Result delivery from llm_agent — resolve pending future.
    2. New task — run the memory operation.
    """
    _refresh_config()
    data = await request.json()

    # Case 1: result delivery from llm_agent
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
        logger.exception("Unhandled error in memory agent receive: %s", exc)
        task_id = data.get("task_id", "")
        parent_task_id = data.get("parent_task_id")
        error_payload = build_result_request(
            agent_id=_OUR_AGENT_ID,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=500,
            output=AgentOutput(content=f"Memory agent error: {exc}"),
        )
        return JSONResponse(status_code=200, content=error_payload)
