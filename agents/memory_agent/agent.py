"""
agents/memory_agent/agent.py — LanceDB-backed long-term memory agent (embedded).

Supports two operations dispatched via the 'operation' field in payload:
  - add:    Ingest content into long-term memory for a user.
  - search: Retrieve the most relevant memories for a user given a query.

Loaded in-process by the router via ASGI transport.

The memory pipeline uses a two-pass LLM approach (fact extraction then
consolidation) backed by a local LanceDB vector store — no external
database server required.  See memory_store.py for details.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import sys
from functools import partial
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# Allow importing helper.py from the project root.
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from helper import AgentInfo, AgentOutput, build_result_request

# ---------------------------------------------------------------------------
# Configuration — read directly from config.json (hot-reloadable)
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


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


# Connection params — read once at singleton init (not hot-reloadable).
LLM_BASE_URL: str = _cfg("MEM0_LLM_BASE_URL", "http://172.23.90.91:8000/api/v1")
LLM_API_KEY: str = _cfg("MEM0_LLM_API_KEY", "placeholder")
LLM_MODEL: str = _cfg("MEM0_LLM_MODEL", "extra.gpt-oss-20b-GGUF")

EMBED_BASE_URL: str = _cfg("MEM0_EMBED_BASE_URL", "http://172.23.90.91:8000/api/v1")
EMBED_API_KEY: str = _cfg("MEM0_EMBED_API_KEY", "placeholder")
EMBED_MODEL: str = _cfg("MEM0_EMBED_MODEL", "Qwen3-Embedding-4B-GGUF")

EMBEDDING_DIMS: int = int(_cfg("MEM0_EMBEDDING_DIMS", "768") or "768")
COLLECTION_NAME: str = _cfg("MEM0_COLLECTION_NAME", "memories")

DEFAULT_SEARCH_COUNT: int = int(_cfg("MEM0_DEFAULT_SEARCH_COUNT", "5"))


def _refresh_config() -> None:
    """Re-read config.json and update hot-reloadable variables."""
    global DEFAULT_SEARCH_COUNT
    cfg = _load_config()
    DEFAULT_SEARCH_COUNT = int(cfg.get("MEM0_DEFAULT_SEARCH_COUNT", 5))


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
    input_schema="operation: str, content: str, user_id: str, count: Optional[int]",
    output_schema="content: str",
    required_input=["operation", "content", "user_id"],
)

# ---------------------------------------------------------------------------
# MemoryStore singleton — lazily initialised on first use so that startup
# does not block (or fail) when the embedding service is unavailable.
# ---------------------------------------------------------------------------

_AGENT_DATA_DIR = Path(__file__).resolve().parent / "data"
_AGENT_DATA_DIR.mkdir(parents=True, exist_ok=True)

_store = None
_store_init_done: bool = False
_store_init_lock = threading.Lock()


def _get_store():
    """Return the MemoryStore singleton, creating it on first call."""
    global _store, _store_init_done
    if not _store_init_done:
        with _store_init_lock:
            if not _store_init_done:
                # Dynamic import: the router loads this module via
                # importlib.util.spec_from_file_location, so relative/package
                # imports are unreliable.  Use a direct path-based import.
                import importlib.util as _ilu
                _ms_path = str(Path(__file__).resolve().parent / "memory_store.py")
                _ms_spec = _ilu.spec_from_file_location("memory_store", _ms_path)
                _ms_mod = _ilu.module_from_spec(_ms_spec)  # type: ignore
                _ms_spec.loader.exec_module(_ms_mod)  # type: ignore
                MemoryStore = _ms_mod.MemoryStore

                _store = MemoryStore(
                    db_path=_AGENT_DATA_DIR / "lancedb",
                    table_name=COLLECTION_NAME,
                    llm_base_url=LLM_BASE_URL,
                    llm_api_key=LLM_API_KEY,
                    llm_model=LLM_MODEL,
                    embed_base_url=EMBED_BASE_URL,
                    embed_api_key=EMBED_API_KEY,
                    embed_model=EMBED_MODEL,
                    embedding_dims=EMBEDDING_DIMS,
                )
                _store_init_done = True
                logger.info(
                    "MemoryStore initialised (db=%s, table=%s, dims=%d)",
                    _AGENT_DATA_DIR / "lancedb",
                    COLLECTION_NAME,
                    EMBEDDING_DIMS,
                )
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
    store = _get_store()

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

    Returns 200 with a routing payload dict so the router can process
    the result delivery.
    """
    _refresh_config()
    data = await request.json()
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
