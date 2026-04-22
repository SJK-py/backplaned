"""
mcp_agent/main.py — MCP wrapper agent (external, always-running).

Connects to one or more MCP servers, auto-derives its AgentInfo from the
MCP tool schemas, and translates router payloads into MCP tool calls.
Includes an admin frontend for managing MCP connections and router
onboarding.

Run:
    cd mcp_agent
    uvicorn main:app --host 0.0.0.0 --port 8082

Environment: mcp_agent/.env
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets as _secrets
import sys
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx
from dotenv import load_dotenv
from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from helper import (
    AgentInfo,
    AgentOutput,
    OnboardResponse,
    PasswordFile,
    build_result_request,
    build_spawn_request,
    onboard,
)
from config_ui import add_config_routes

load_dotenv(Path(__file__).parent / "data" / ".env")

import os

from agent_info_builder import (
    ToolStore,
    build_tool_detail,
    build_tools_markdown,
    derive_agent_info,
    get_uncached_tools,
    get_uncached_doc_tools,
    _build_brief_prompt,
    _build_doc_prompt,
)
from mcp_manager import MCPManager, MCPServerConfig

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOST: str = os.environ.get("AGENT_HOST", "0.0.0.0")
PORT: int = int(os.environ.get("AGENT_PORT", "8082"))
ADMIN_PASSWORD: str = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    import warnings as _w
    _w.warn("ADMIN_PASSWORD is not set — web UI login will be unavailable until configured", stacklevel=1)
SESSION_SECRET: str = os.environ.get("SESSION_SECRET", _secrets.token_hex(32))

ROUTER_URL: str = os.environ.get("ROUTER_URL", "http://localhost:8000").rstrip("/")
INVITATION_TOKEN: str = os.environ.get("INVITATION_TOKEN", "")
AGENT_URL: str = os.environ.get("AGENT_URL") or f"http://localhost:{PORT}"
ENDPOINT_URL: str = f"{AGENT_URL}/receive"

DATA_DIR: Path = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent / "data")))
CONFIG_FILE: Path = DATA_DIR / "config.json"
CREDENTIALS_FILE: Path = DATA_DIR / "credentials.json"
BRIEF_CACHE_FILE: Path = DATA_DIR / "brief_cache.json"
LOG_CAPACITY: int = int(os.environ.get("LOG_CAPACITY", "500"))
RECONNECT_INTERVAL: int = int(os.environ.get("RECONNECT_INTERVAL", "60"))
LLM_AGENT_ID: str = os.environ.get("LLM_AGENT_ID", "llm_agent")
LLM_MODEL_ID: str = os.environ.get("LLM_MODEL_ID", "") or ""

DATA_DIR.mkdir(parents=True, exist_ok=True)

_admin_pw = PasswordFile(DATA_DIR / "admin_password.json", ADMIN_PASSWORD)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("mcp_agent")

_log_ring: deque[str] = deque(maxlen=LOG_CAPACITY)


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _log_ring.append(self.format(record))


_ring_handler = _RingHandler()
_ring_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_ring_handler)

# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

_signer = URLSafeTimedSerializer(SESSION_SECRET)
SESSION_COOKIE = "mcp_session"
SESSION_MAX_AGE = 3600 * 8


def _make_session_token() -> str:
    return _signer.dumps("authenticated")


def _verify_session_token(token: str) -> bool:
    try:
        _signer.loads(token, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _require_auth(mcp_session: Optional[str] = Cookie(default=None)) -> None:
    if not mcp_session or not _verify_session_token(mcp_session):
        raise HTTPException(status_code=401, detail="Not authenticated")


# ---------------------------------------------------------------------------
# Router credentials management
# ---------------------------------------------------------------------------

_agent_id: Optional[str] = None
_auth_token: Optional[str] = None
_http_client: Optional[httpx.AsyncClient] = None


def _load_credentials() -> Optional[dict]:
    if CREDENTIALS_FILE.exists():
        try:
            return json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _save_credentials(agent_id: str, auth_token: str) -> None:
    CREDENTIALS_FILE.write_text(
        json.dumps({"agent_id": agent_id, "auth_token": auth_token}),
        encoding="utf-8",
    )


async def _ensure_registered() -> None:
    global _agent_id, _auth_token, _http_client

    creds = _load_credentials()
    if creds:
        # Verify still registered using the agent's own auth token.
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(
                    f"{ROUTER_URL}/agent/destinations",
                    headers={"Authorization": f"Bearer {creds['auth_token']}"},
                )
                if r.status_code == 200:
                    _agent_id = creds["agent_id"]
                    _auth_token = creds["auth_token"]
                    if _http_client:
                        await _http_client.aclose()
                    _http_client = httpx.AsyncClient(
                        headers={"Authorization": f"Bearer {_auth_token}"},
                        timeout=120.0,
                    )
                    try:
                        await _http_client.put(
                            f"{ROUTER_URL}/agent-info",
                            json={"agent_id": _agent_id, "endpoint_url": ENDPOINT_URL},
                            timeout=10.0,
                        )
                    except Exception:
                        pass
                    logger.info("Router credentials reloaded for %s", _agent_id)
                    return
                # 401/403 means credentials are invalid — fall through to re-onboard.
        except Exception as exc:
            logger.warning("Could not verify router credentials: %s — using saved credentials", exc)
            _agent_id = creds["agent_id"]
            _auth_token = creds["auth_token"]
            _http_client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {_auth_token}"},
                timeout=120.0,
            )
            return

    if not INVITATION_TOKEN:
        logger.error(
            "INVITATION_TOKEN not set and no valid saved credentials — "
            "agent will run without router connection."
        )
        return

    await _do_register(INVITATION_TOKEN)


async def _do_register(invitation_token: str) -> bool:
    """Register with the router using the given invitation token. Returns True on success."""
    global _agent_id, _auth_token, _http_client

    info_dict = derive_agent_info("mcp_agent", _mcp_manager.get_all_tools(), _brief_cache)
    agent_info = AgentInfo(**info_dict)

    try:
        resp: OnboardResponse = await onboard(
            router_url=ROUTER_URL,
            invitation_token=invitation_token,
            endpoint_url=ENDPOINT_URL,
            agent_info=agent_info,
        )
        _agent_id = resp.agent_id
        _auth_token = resp.auth_token
        _save_credentials(_agent_id, _auth_token)
        _http_client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {_auth_token}"},
            timeout=120.0,
        )
        logger.info("Registered with router as %s", _agent_id)
        return True
    except Exception as exc:
        logger.error("Router onboarding failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Brief description cache + LLM generation
# ---------------------------------------------------------------------------

_brief_cache = ToolStore(BRIEF_CACHE_FILE)


def _extract_llm_text(data: dict) -> str:
    """Extract the text content from an llm_agent result delivery."""
    payload = data.get("payload", {})
    raw = payload.get("content", "") if isinstance(payload, dict) else ""
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
        return parsed.get("content", raw) if isinstance(parsed, dict) else raw
    except (json.JSONDecodeError, TypeError):
        return raw


# Pending brief-generation futures: identifier -> asyncio.Future
_brief_futures: dict[str, asyncio.Future] = {}
# Pending doc-generation futures: identifier -> asyncio.Future
_doc_futures: dict[str, asyncio.Future] = {}


async def _generate_briefs(tools: list) -> None:
    """
    Call the LLM agent via the router to generate one-line tool summaries.
    Results arrive asynchronously via /receive and are matched by identifier.
    """
    if not _agent_id or not _http_client:
        logger.warning("Cannot generate briefs — not connected to router")
        return

    from mcp_manager import ToolDef

    pending: list[tuple[ToolDef, str, asyncio.Future]] = []

    for tool in tools:
        prompt = _build_brief_prompt(tool)
        identifier = f"brief:{tool.namespaced_name}"
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        _brief_futures[identifier] = fut

        body = build_spawn_request(
            agent_id=_agent_id,
            identifier=identifier,
            parent_task_id=None,
            destination_agent_id=LLM_AGENT_ID,
            payload={
                "llmcall": {
                    "messages": [
                        {"role": "system", "content": (
                            "You are a concise technical writer. Output ONLY the single "
                            "formatted line requested. No preamble, no explanation."
                        )},
                        {"role": "user", "content": prompt},
                    ],
                    "tools": [],
                    **({"model_id": LLM_MODEL_ID} if LLM_MODEL_ID else {}),
                },
            },
        )
        try:
            resp = await _http_client.post(
                f"{ROUTER_URL}/route", json=body, timeout=30.0,
            )
            if resp.status_code not in (200, 202):
                logger.warning(
                    "Failed to spawn brief generation for %s: HTTP %d",
                    tool.namespaced_name, resp.status_code,
                )
                _brief_futures.pop(identifier, None)
                fut.cancel()
                continue
            pending.append((tool, identifier, fut))
        except Exception as exc:
            logger.warning("Brief generation failed for %s: %s", tool.namespaced_name, exc)
            _brief_futures.pop(identifier, None)
            fut.cancel()

    # Wait for all pending results concurrently (single shared timeout).
    # Timeout is generous to accommodate bursts when many MCP servers
    # connect simultaneously and flood the LLM agent queue.
    if pending:
        futs = [fut for _, _, fut in pending]
        done, not_done = await asyncio.wait(futs, timeout=90.0)
        for fut in not_done:
            fut.cancel()
        for tool, identifier, fut in pending:
            try:
                if fut in done and not fut.cancelled():
                    brief_text = fut.result()
                    if brief_text:
                        line = brief_text.strip().split("\n")[0].strip()
                        if line:
                            _brief_cache.put_llm_brief(tool, line)
                            logger.info("Brief for %s: %s", tool.namespaced_name, line)
                else:
                    logger.warning("Brief generation timed out for %s", tool.namespaced_name)
            except (asyncio.CancelledError, Exception) as exc:
                logger.warning("Brief generation error for %s: %s", tool.namespaced_name, exc)
            finally:
                _brief_futures.pop(identifier, None)


async def _generate_docs(tools: list) -> None:
    """
    Call the LLM agent via the router to generate comprehensive tool documentation.
    Results arrive asynchronously via /receive and are matched by identifier.
    """
    if not _agent_id or not _http_client:
        logger.warning("Cannot generate docs — not connected to router")
        return

    from mcp_manager import ToolDef

    pending: list[tuple[ToolDef, str, asyncio.Future]] = []

    for tool in tools:
        prompt = _build_doc_prompt(tool)
        identifier = f"doc:{tool.namespaced_name}"
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        _doc_futures[identifier] = fut

        body = build_spawn_request(
            agent_id=_agent_id,
            identifier=identifier,
            parent_task_id=None,
            destination_agent_id=LLM_AGENT_ID,
            payload={
                "llmcall": {
                    "messages": [
                        {"role": "system", "content": (
                            "You are a technical writer. Write clear, comprehensive "
                            "Markdown documentation as requested. No preamble."
                        )},
                        {"role": "user", "content": prompt},
                    ],
                    "tools": [],
                    **({"model_id": LLM_MODEL_ID} if LLM_MODEL_ID else {}),
                },
            },
        )
        try:
            resp = await _http_client.post(
                f"{ROUTER_URL}/route", json=body, timeout=30.0,
            )
            if resp.status_code not in (200, 202):
                logger.warning(
                    "Failed to spawn doc generation for %s: HTTP %d",
                    tool.namespaced_name, resp.status_code,
                )
                _doc_futures.pop(identifier, None)
                fut.cancel()
                continue
            pending.append((tool, identifier, fut))
        except Exception as exc:
            logger.warning("Doc generation failed for %s: %s", tool.namespaced_name, exc)
            _doc_futures.pop(identifier, None)
            fut.cancel()

    # Wait for all pending results concurrently (single shared timeout).
    # Generous timeout for doc generation — these are longer LLM calls
    # and many may be queued during bulk MCP server connections.
    if pending:
        futs = [fut for _, _, fut in pending]
        done, not_done = await asyncio.wait(futs, timeout=300.0)
        for fut in not_done:
            fut.cancel()
        for tool, identifier, fut in pending:
            try:
                if fut in done and not fut.cancelled():
                    doc_text = fut.result()
                    if doc_text and doc_text.strip():
                        _brief_cache.put_llm_doc(tool.namespaced_name, doc_text.strip())
                        logger.info("Doc generated for %s (%d chars)", tool.namespaced_name, len(doc_text))
                else:
                    logger.warning("Doc generation timed out for %s", tool.namespaced_name)
            except (asyncio.CancelledError, Exception) as exc:
                logger.warning("Doc generation error for %s: %s", tool.namespaced_name, exc)
            finally:
                _doc_futures.pop(identifier, None)


# ---------------------------------------------------------------------------
# AgentInfo refresh
# ---------------------------------------------------------------------------

_last_tool_snapshot: Optional[str] = None


async def _refresh_router_agent_info() -> bool:
    """
    Re-derive AgentInfo from current MCP tools and push to the router.
    Only pushes if the tool set has actually changed.
    Returns True if an update was pushed.
    """
    global _last_tool_snapshot

    if not _agent_id or not _http_client:
        return False

    tools = _mcp_manager.get_all_tools()
    snapshot = json.dumps(
        [(t.namespaced_name, t.description) for t in tools],
        sort_keys=True,
    )

    if snapshot == _last_tool_snapshot:
        return False

    # Generate briefs for any new/changed tools via LLM (non-blocking best-effort).
    uncached = get_uncached_tools(tools, _brief_cache)
    if uncached:
        await _generate_briefs(uncached)

    # Generate docs for any tools missing LLM documentation.
    uncached_docs = get_uncached_doc_tools(tools, _brief_cache)
    if uncached_docs:
        await _generate_docs(uncached_docs)

    _brief_cache.prune({t.namespaced_name for t in tools})

    info = derive_agent_info(_agent_id, tools, _brief_cache)

    # Build documentation URL with cache-busting param so router re-fetches content.
    import time
    doc_url = f"{AGENT_URL}/docs/tools.md?t={int(time.time())}"

    try:
        resp = await _http_client.put(
            f"{ROUTER_URL}/agent-info",
            json={
                "agent_id": _agent_id,
                "description": info["description"],
                "input_schema": info["input_schema"],
                "output_schema": info["output_schema"],
                "required_input": info["required_input"],
                "documentation_url": doc_url,
            },
        )
        resp.raise_for_status()
        _last_tool_snapshot = snapshot
        logger.info("AgentInfo refreshed on router (%d tools)", len(tools))
        return True
    except Exception as exc:
        logger.error("Failed to refresh AgentInfo on router: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Task handling
# ---------------------------------------------------------------------------


async def _handle_task(data: dict[str, Any]) -> None:
    """Process an inbound task from the router."""
    task_id: str = data.get("task_id", "")
    parent_task_id: Optional[str] = data.get("parent_task_id")
    payload: dict[str, Any] = data.get("payload", {})

    tool_name: str = payload.get("tool_name", "")
    arguments: dict[str, Any] = payload.get("arguments", {})

    if not tool_name:
        await _report_result(
            task_id, parent_task_id, 400,
            "Missing 'tool_name' in payload. Send tool_name (str) and arguments (dict).",
        )
        return

    if not isinstance(arguments, dict):
        # Try to parse if it's a JSON string.
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                await _report_result(
                    task_id, parent_task_id, 400,
                    f"'arguments' must be a dict, got: {type(arguments).__name__}",
                )
                return
        else:
            await _report_result(
                task_id, parent_task_id, 400,
                f"'arguments' must be a dict, got: {type(arguments).__name__}",
            )
            return

    logger.info("Task %s: calling tool '%s'", task_id[:12], tool_name)

    # Built-in "details" command: return comprehensive docs for a tool.
    if tool_name == "details":
        target = arguments.get("tool", "")
        if not target:
            # List all tools briefly.
            tools = _mcp_manager.get_all_tools()
            lines = [f"Available tools ({len(tools)}):"]
            for t in tools:
                brief = _brief_cache.get_brief(t) or f"{t.namespaced_name} — {t.description[:60]}"
                lines.append(f"  {brief}")
            lines.append('\nUse tool_name="details" arguments={"tool": "<name>"} for full docs.')
            await _report_result(task_id, parent_task_id, 200, "\n".join(lines))
            return

        # Find the tool by name (support partial match).
        tools = _mcp_manager.get_all_tools()
        match = None
        for t in tools:
            if t.namespaced_name == target or t.name == target:
                match = t
                break
        if not match:
            # Try substring match.
            candidates = [t for t in tools if target.lower() in t.namespaced_name.lower()]
            if len(candidates) == 1:
                match = candidates[0]
            elif candidates:
                names = ", ".join(t.namespaced_name for t in candidates)
                await _report_result(
                    task_id, parent_task_id, 400,
                    f"Ambiguous tool name '{target}'. Matches: {names}",
                )
                return
            else:
                available = ", ".join(t.namespaced_name for t in tools)
                await _report_result(
                    task_id, parent_task_id, 400,
                    f"Tool '{target}' not found. Available: {available}",
                )
                return

        detail_md = build_tool_detail(match, _brief_cache)
        await _report_result(task_id, parent_task_id, 200, detail_md)
        return

    try:
        result_text = await _mcp_manager.call_tool(tool_name, arguments)
        await _report_result(task_id, parent_task_id, 200, result_text)
    except KeyError as exc:
        await _report_result(task_id, parent_task_id, 400, str(exc))
    except RuntimeError as exc:
        status = 504 if "timed out" in str(exc) else 500
        await _report_result(task_id, parent_task_id, status, str(exc))
    except Exception as exc:
        await _report_result(
            task_id, parent_task_id, 500,
            f"Tool call failed: {type(exc).__name__}: {exc}",
        )


async def _report_result(
    task_id: str,
    parent_task_id: Optional[str],
    status_code: int,
    content: str,
) -> None:
    """Report a task result back to the router."""
    if not _agent_id or not _http_client:
        logger.error("Cannot report result — not connected to router")
        return

    body = build_result_request(
        agent_id=_agent_id,
        task_id=task_id,
        parent_task_id=parent_task_id,
        status_code=status_code,
        output=AgentOutput(content=content),
    )
    try:
        resp = await _http_client.post(f"{ROUTER_URL}/route", json=body)
        if resp.status_code not in (200, 202):
            logger.warning(
                "Router returned %d when reporting result for task %s",
                resp.status_code, task_id[:12],
            )
    except Exception as exc:
        logger.error("Failed to report result for task %s: %s", task_id[:12], exc)


# ---------------------------------------------------------------------------
# Background reconnection loop
# ---------------------------------------------------------------------------

_reconnect_task: Optional[asyncio.Task] = None


async def _reconnect_loop() -> None:
    """Periodically reconnect failed/disconnected MCP servers."""
    while True:
        await asyncio.sleep(RECONNECT_INTERVAL)
        changed = False
        for name in _mcp_manager.get_server_names():
            states = _mcp_manager.get_server_states()
            state = states.get(name, {})
            cfg = state.get("config", {})
            if cfg.get("enabled") and state.get("status") in ("error", "disconnected"):
                logger.info("Attempting reconnection to MCP server '%s'", name)
                await _mcp_manager.reconnect_server(name)
                new_state = _mcp_manager.get_server_states().get(name, {})
                if new_state.get("status") == "connected":
                    changed = True

        if changed:
            await _refresh_router_agent_info()


# ---------------------------------------------------------------------------
# MCP Manager instance
# ---------------------------------------------------------------------------

_mcp_manager = MCPManager(CONFIG_FILE)


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global _reconnect_task

    await _ensure_registered()
    await _mcp_manager.connect_all()
    await _refresh_router_agent_info()

    _reconnect_task = asyncio.create_task(_reconnect_loop())

    yield

    _reconnect_task.cancel()
    try:
        await _reconnect_task
    except asyncio.CancelledError:
        pass

    await _mcp_manager.disconnect_all()
    if _http_client:
        await _http_client.aclose()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="MCP Agent", lifespan=lifespan)

_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


def _static_version() -> str:
    """Compute a short cache-busting version from static file mtimes."""
    import hashlib
    h = hashlib.md5(usedforsecurity=False)
    for f in sorted(_static_dir.iterdir()):
        if f.is_file():
            h.update(str(f.stat().st_mtime_ns).encode())
    return h.hexdigest()[:8]


_STATIC_VER: str = _static_version() if _static_dir.exists() else ""


# ---------------------------------------------------------------------------
# Router callback endpoint
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "agent_id": _agent_id or "not initialized"}


@app.post("/receive")
async def receive(request: Request) -> JSONResponse:
    """Router delivers task payloads here."""
    # Verify delivery auth from router
    if _auth_token:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not _secrets.compare_digest(auth[7:], _auth_token):
            return JSONResponse(status_code=403, content={"error": "Forbidden"})

    data = await request.json()

    # Check if this is a result callback for a brief/doc generation request.
    identifier = data.get("identifier", "")
    if identifier and identifier.startswith("brief:") and identifier in _brief_futures:
        content = _extract_llm_text(data)
        fut = _brief_futures.get(identifier)
        if fut and not fut.done():
            fut.set_result(content)
        return JSONResponse(status_code=202, content={"status": "accepted"})

    if identifier and identifier.startswith("doc:") and identifier in _doc_futures:
        content = _extract_llm_text(data)
        fut = _doc_futures.get(identifier)
        if fut and not fut.done():
            fut.set_result(content)
        return JSONResponse(status_code=202, content={"status": "accepted"})

    asyncio.create_task(_handle_task(data))
    return JSONResponse(status_code=202, content={"status": "accepted"})


# ---------------------------------------------------------------------------
# Dynamic tool documentation
# ---------------------------------------------------------------------------


@app.get("/docs/tools.md")
async def tools_documentation() -> PlainTextResponse:
    """Serve dynamically generated tool documentation."""
    tools = _mcp_manager.get_all_tools()
    md = build_tools_markdown(tools, _brief_cache)
    return PlainTextResponse(md, media_type="text/markdown")


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------


@app.post("/ui/login")
async def login(request: Request, response: Response) -> dict:
    body = await request.json()
    password = body.get("password", "")
    if not password or not _admin_pw.verify(password):
        raise HTTPException(status_code=403, detail="Invalid password")
    token = _make_session_token()
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_MAX_AGE, httponly=True, samesite="lax",
    )
    return {"status": "ok"}


@app.post("/ui/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "ok"}


@app.get("/ui/whoami")
async def whoami(mcp_session: Optional[str] = Cookie(default=None)) -> dict:
    return {"authenticated": bool(mcp_session and _verify_session_token(mcp_session))}


@app.post("/ui/change-password")
async def change_password(request: Request, mcp_session: Optional[str] = Cookie(default=None)) -> dict:
    _require_auth(mcp_session)
    body = await request.json()
    current = body.get("current_password", "")
    new_pw = body.get("new_password", "")
    if not new_pw or len(new_pw) < 4:
        raise HTTPException(status_code=400, detail="New password must be at least 4 characters")
    if not _admin_pw.verify(current):
        raise HTTPException(status_code=403, detail="Current password is incorrect")
    _admin_pw.change(new_pw)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------


@app.get("/ui/status")
async def ui_status(mcp_session: Optional[str] = Cookie(default=None)) -> dict:
    _require_auth(mcp_session)
    servers = _mcp_manager.get_server_states()
    connected_count = sum(1 for s in servers.values() if s["status"] == "connected")
    total_tools = sum(s["tool_count"] for s in servers.values())
    info = derive_agent_info(_agent_id or "mcp_agent", _mcp_manager.get_all_tools(), _brief_cache)
    router_connected = False
    if _http_client and _agent_id:
        try:
            r = await _http_client.get(f"{ROUTER_URL}/health", timeout=3.0)
            router_connected = r.status_code == 200
        except Exception:
            pass
    return {
        "router_connected": router_connected,
        "agent_id": _agent_id,
        "router_url": ROUTER_URL,
        "servers_total": len(servers),
        "servers_connected": connected_count,
        "total_tools": total_tools,
        "agent_info": info,
    }


# ---------------------------------------------------------------------------
# MCP Server management endpoints
# ---------------------------------------------------------------------------


class AddServerRequest(BaseModel):
    name: str
    transport_type: Optional[str] = None
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    url: str = ""
    headers: dict[str, str] = {}
    enabled_tools: list[str] = ["*"]
    tool_timeout: int = 30
    enabled: bool = True


class UpdateServerRequest(BaseModel):
    transport_type: Optional[str] = None
    command: Optional[str] = None
    args: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None
    url: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    enabled_tools: Optional[list[str]] = None
    tool_timeout: Optional[int] = None
    enabled: Optional[bool] = None


@app.get("/ui/servers")
async def ui_list_servers(mcp_session: Optional[str] = Cookie(default=None)) -> Any:
    _require_auth(mcp_session)
    return _mcp_manager.get_server_states()


@app.post("/ui/servers")
async def ui_add_server(
    body: AddServerRequest,
    mcp_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(mcp_session)
    try:
        config = MCPServerConfig(**body.model_dump())
        _mcp_manager.add_server(config)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    if config.enabled:
        await _mcp_manager.connect_server(config.name)
        await _refresh_router_agent_info()

    return {"status": "added", "name": config.name}


@app.put("/ui/servers/{name}")
async def ui_update_server(
    name: str,
    body: UpdateServerRequest,
    mcp_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(mcp_session)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return {"status": "no_changes"}
    try:
        _mcp_manager.update_server_config(name, **updates)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found.")

    # Reconnect to apply changes.
    await _mcp_manager.reconnect_server(name)
    await _refresh_router_agent_info()
    return {"status": "updated", "name": name}


@app.delete("/ui/servers/{name}")
async def ui_remove_server(
    name: str,
    mcp_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(mcp_session)
    await _mcp_manager.disconnect_server(name)
    _mcp_manager.remove_server_config(name)
    await _refresh_router_agent_info()
    return {"status": "removed", "name": name}


@app.post("/ui/servers/{name}/reconnect")
async def ui_reconnect_server(
    name: str,
    mcp_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(mcp_session)
    await _mcp_manager.reconnect_server(name)
    await _refresh_router_agent_info()
    state = _mcp_manager.get_server_states().get(name, {})
    return {"status": state.get("status", "unknown"), "error": state.get("error")}


@app.post("/ui/servers/{name}/toggle")
async def ui_toggle_server(
    name: str,
    mcp_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(mcp_session)
    states = _mcp_manager.get_server_states()
    if name not in states:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found.")

    current_enabled = states[name]["config"].get("enabled", True)
    new_enabled = not current_enabled
    _mcp_manager.update_server_config(name, enabled=new_enabled)

    if new_enabled:
        await _mcp_manager.connect_server(name)
    else:
        await _mcp_manager.disconnect_server(name)

    await _refresh_router_agent_info()
    return {"status": "toggled", "enabled": new_enabled}


# ---------------------------------------------------------------------------
# Tool endpoints
# ---------------------------------------------------------------------------


@app.get("/ui/servers/{name}/tools")
async def ui_server_tools(
    name: str,
    mcp_session: Optional[str] = Cookie(default=None),
) -> Any:
    _require_auth(mcp_session)
    tools = _mcp_manager.get_server_tools(name)
    return [
        {
            "name": t.namespaced_name,
            "raw_name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in tools
    ]


class TestToolRequest(BaseModel):
    arguments: dict[str, Any] = {}


@app.post("/ui/servers/{name}/tools/{tool_name}/test")
async def ui_test_tool(
    name: str,
    tool_name: str,
    body: TestToolRequest,
    mcp_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(mcp_session)
    namespaced = f"{name}__{tool_name}"
    try:
        result = await _mcp_manager.call_tool(namespaced, body.arguments)
        return {"status": "ok", "result": result}
    except KeyError as exc:
        return {"status": "error", "result": str(exc)}
    except Exception as exc:
        return {"status": "error", "result": f"{type(exc).__name__}: {exc}"}


@app.get("/ui/tools")
async def ui_all_tools(mcp_session: Optional[str] = Cookie(default=None)) -> Any:
    _require_auth(mcp_session)
    tools = _mcp_manager.get_all_tools()
    result = []
    for t in tools:
        entry = _brief_cache.get_brief_entry(t)
        doc_entry = _brief_cache.get_doc_entry(t.namespaced_name)
        result.append({
            "name": t.namespaced_name,
            "raw_name": t.name,
            "server": t.server_name,
            "description": t.description,
            "input_schema": t.input_schema,
            "brief": _brief_cache.get_brief(t),
            "llm_brief": entry.get("llm") if entry else None,
            "admin_brief": entry.get("admin") if entry else None,
            "llm_doc": doc_entry.get("llm") if doc_entry else None,
            "admin_doc": doc_entry.get("admin") if doc_entry else None,
        })
    return result


# ---------------------------------------------------------------------------
# Tool description admin endpoints
# ---------------------------------------------------------------------------


class SetBriefRequest(BaseModel):
    tool_name: str
    brief: Optional[str] = None  # None to clear admin override


class SetDocRequest(BaseModel):
    tool_name: str
    doc: Optional[str] = None  # None to clear


@app.post("/ui/tools/brief")
async def ui_set_tool_brief(
    body: SetBriefRequest,
    mcp_session: Optional[str] = Cookie(default=None),
) -> dict:
    """Set or clear the admin brief override for a tool."""
    _require_auth(mcp_session)
    found = _brief_cache.set_admin_brief(body.tool_name, body.brief)
    if not found:
        raise HTTPException(status_code=404, detail=f"Tool '{body.tool_name}' not found in cache.")
    # Push updated AgentInfo to router.
    global _last_tool_snapshot
    _last_tool_snapshot = None
    await _refresh_router_agent_info()
    return {"status": "ok", "tool_name": body.tool_name}


@app.post("/ui/tools/doc")
async def ui_set_tool_doc(
    body: SetDocRequest,
    mcp_session: Optional[str] = Cookie(default=None),
) -> dict:
    """Set or clear admin documentation for a tool."""
    _require_auth(mcp_session)
    _brief_cache.set_doc(body.tool_name, body.doc)
    return {"status": "ok", "tool_name": body.tool_name}


@app.get("/ui/tools/{tool_name}/detail")
async def ui_tool_detail(
    tool_name: str,
    mcp_session: Optional[str] = Cookie(default=None),
) -> dict:
    """Get full detail for a tool (same as the 'details' command)."""
    _require_auth(mcp_session)
    tools = _mcp_manager.get_all_tools()
    for t in tools:
        if t.namespaced_name == tool_name:
            return {"detail": build_tool_detail(t, _brief_cache)}
    raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found.")


# ---------------------------------------------------------------------------
# Agent info endpoints
# ---------------------------------------------------------------------------


@app.get("/ui/agent-info")
async def ui_agent_info(mcp_session: Optional[str] = Cookie(default=None)) -> dict:
    _require_auth(mcp_session)
    tools = _mcp_manager.get_all_tools()
    info = derive_agent_info(_agent_id or "mcp_agent", tools, _brief_cache)
    return {
        "agent_info": info,
        "tool_count": len(tools),
        "last_snapshot": _last_tool_snapshot is not None,
    }


@app.post("/refresh-info")
async def refresh_info(request: Request) -> JSONResponse:
    """Re-push this agent's AgentInfo to the router (called by router admin)."""
    if _auth_token:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not _secrets.compare_digest(auth[7:], _auth_token):
            return JSONResponse(status_code=403, content={"error": "Forbidden"})
    global _last_tool_snapshot
    _last_tool_snapshot = None  # Force refresh.
    updated = await _refresh_router_agent_info()
    return JSONResponse({"status": "refreshed" if updated else "no_change"})


@app.post("/ui/agent-info/refresh")
async def ui_refresh_agent_info(mcp_session: Optional[str] = Cookie(default=None)) -> dict:
    _require_auth(mcp_session)
    global _last_tool_snapshot
    _last_tool_snapshot = None  # Force refresh.
    updated = await _refresh_router_agent_info()
    return {"status": "refreshed" if updated else "no_change"}


# ---------------------------------------------------------------------------
# Onboarding endpoints
# ---------------------------------------------------------------------------


@app.get("/ui/onboarding")
async def ui_onboarding(mcp_session: Optional[str] = Cookie(default=None)) -> dict:
    _require_auth(mcp_session)
    router_reachable = False
    if _http_client and _agent_id:
        try:
            r = await _http_client.get(f"{ROUTER_URL}/health", timeout=3.0)
            router_reachable = r.status_code == 200
        except Exception:
            pass
    return {
        "router_url": ROUTER_URL,
        "agent_id": _agent_id,
        "connected": router_reachable,
    }


class RegisterRequest(BaseModel):
    invitation_token: str


@app.post("/ui/onboarding/register")
async def ui_register(
    body: RegisterRequest,
    mcp_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(mcp_session)
    success = await _do_register(body.invitation_token)
    if success:
        await _refresh_router_agent_info()
        return {"status": "registered", "agent_id": _agent_id}
    raise HTTPException(status_code=502, detail="Registration failed. Check logs.")


# ---------------------------------------------------------------------------
# Log endpoint
# ---------------------------------------------------------------------------


@app.get("/ui/logs")
async def ui_logs(mcp_session: Optional[str] = Cookie(default=None)) -> list[str]:
    _require_auth(mcp_session)
    return list(_log_ring)


add_config_routes(app, Path(__file__).resolve().parent, _require_auth, cookie_name="mcp_session")

# ---------------------------------------------------------------------------
# Root — serve the SPA
# ---------------------------------------------------------------------------


@app.get("/")
async def root():
    html = (_static_dir / "index.html").read_text(encoding="utf-8")
    if _STATIC_VER:
        html = html.replace("/static/style.css", f"/static/style.css?v={_STATIC_VER}")
        html = html.replace("/static/app.js", f"/static/app.js?v={_STATIC_VER}")
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=HOST, port=int(PORT), reload=False)
