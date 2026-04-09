"""
mcp_server/main.py — Router-as-MCP-Server.

A thin MCP server that introspects ``/admin/agents`` and exposes every
registered router agent as an MCP tool.  One MCP connection gives a
host model (Claude Desktop, Cursor, …) access to all agents
simultaneously.

Includes:
  * Admin frontend for configuration and monitoring
  * Background agent-discovery poller
  * MCP SSE / streamable-HTTP transport on a separate port

Run:
    cd mcp_server
    python main.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets as _secrets
import sys
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from helper import AgentInfo, AgentOutput, OnboardResponse, PasswordFile, build_spawn_request, onboard
from config_ui import add_config_routes

load_dotenv(Path(__file__).parent / "data" / ".env")

from mcp_bridge import MCPBridge

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOST: str = os.environ.get("AGENT_HOST", "0.0.0.0")
PORT: int = int(os.environ.get("AGENT_PORT", "8083"))
ADMIN_PASSWORD: str = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    import warnings as _w
    _w.warn("ADMIN_PASSWORD is not set — web UI login will be unavailable until configured", stacklevel=1)
SESSION_SECRET: str = os.environ.get("SESSION_SECRET", _secrets.token_hex(32))

ROUTER_URL: str = os.environ.get("ROUTER_URL", "http://localhost:8000").rstrip("/")
INVITATION_TOKEN: str = os.environ.get("INVITATION_TOKEN", "")
AGENT_URL: str = os.environ.get("AGENT_URL") or f"http://localhost:{PORT}"
ENDPOINT_URL: str = f"{AGENT_URL}/receive"

MCP_TRANSPORT: str = os.environ.get("MCP_TRANSPORT", "sse")  # "sse" or "streamable-http"
MCP_PORT: int = int(os.environ.get("MCP_PORT", "8084"))
POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL", "30"))
TOOL_TIMEOUT: float = float(os.environ.get("TOOL_TIMEOUT", "60"))

_exclude_raw = os.environ.get("EXCLUDE_AGENTS", "")
EXCLUDE_AGENTS: set[str] = {s.strip() for s in _exclude_raw.split(",") if s.strip()}

DATA_DIR: Path = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent / "data")))
CREDENTIALS_FILE: Path = DATA_DIR / "credentials.json"
CONFIG_FILE: Path = DATA_DIR / "config.json"
LOG_CAPACITY: int = int(os.environ.get("LOG_CAPACITY", "500"))

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
logger = logging.getLogger("mcp_server")

_log_ring: deque[str] = deque(maxlen=LOG_CAPACITY)


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _log_ring.append(self.format(record))


_ring = _RingHandler()
_ring.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_ring)

# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

_signer = URLSafeTimedSerializer(SESSION_SECRET)
SESSION_COOKIE = "mcps_session"
SESSION_MAX_AGE = 3600 * 8


def _make_session_token() -> str:
    return _signer.dumps("authenticated")


def _verify_session_token(token: str) -> bool:
    try:
        _signer.loads(token, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _require_auth(mcps_session: Optional[str] = Cookie(default=None)) -> None:
    if not mcps_session or not _verify_session_token(mcps_session):
        raise HTTPException(status_code=401, detail="Not authenticated")


# ---------------------------------------------------------------------------
# Router credentials
# ---------------------------------------------------------------------------

_agent_id: Optional[str] = None
_auth_token: Optional[str] = None
_http_client: Optional[httpx.AsyncClient] = None


def _load_credentials() -> Optional[dict]:
    if CREDENTIALS_FILE.exists():
        try:
            return json.loads(CREDENTIALS_FILE.read_text("utf-8"))
        except Exception:
            pass
    return None


def _save_credentials(agent_id: str, auth_token: str) -> None:
    CREDENTIALS_FILE.write_text(
        json.dumps({"agent_id": agent_id, "auth_token": auth_token}), "utf-8",
    )


def _load_config() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {}


def _save_config(data: dict[str, Any]) -> None:
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    tmp.rename(CONFIG_FILE)


async def _ensure_registered() -> None:
    global _agent_id, _auth_token, _http_client

    creds = _load_credentials()
    if creds:
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(
                    f"{ROUTER_URL}/agent/destinations",
                    headers={"Authorization": f"Bearer {creds.get('auth_token', '')}"},
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
        except Exception as exc:
            logger.warning("Could not verify credentials: %s — using saved", exc)
            _agent_id = creds["agent_id"]
            _auth_token = creds["auth_token"]
            _http_client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {_auth_token}"},
                timeout=120.0,
            )
            return

    if not INVITATION_TOKEN:
        logger.error(
            "INVITATION_TOKEN not set and no saved credentials — "
            "MCP server will run without router connection."
        )
        return

    await _do_register(INVITATION_TOKEN)


_mcp_server_agent_info = AgentInfo(
    agent_id="mcp_server",
    description=(
        "Inbound MCP bridge (not callable by agents). Exposes router agents "
        "as MCP tools to external MCP clients."
    ),
    input_schema="llmdata: LLMData",
    output_schema="content: str",
    required_input=["llmdata"],
    hidden=True,
)


async def _do_register(invitation_token: str) -> bool:
    global _agent_id, _auth_token, _http_client

    try:
        resp: OnboardResponse = await onboard(
            router_url=ROUTER_URL,
            invitation_token=invitation_token,
            endpoint_url=ENDPOINT_URL,
            agent_info=_mcp_server_agent_info,
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
# Task spawning (used by MCPBridge)
# ---------------------------------------------------------------------------


async def _spawn_task(destination_agent_id: str, payload: dict[str, Any]) -> str:
    """
    Spawn a new task to a destination agent via the router.
    Returns the task_id assigned by the router.
    """
    if not _agent_id or not _http_client:
        raise RuntimeError("Not connected to router")

    identifier = str(uuid.uuid4())
    body = build_spawn_request(
        agent_id=_agent_id,
        identifier=identifier,
        parent_task_id=None,
        destination_agent_id=destination_agent_id,
        payload=payload,
    )
    resp = await _http_client.post(f"{ROUTER_URL}/route", json=body)
    resp.raise_for_status()
    data = resp.json()
    task_id = data.get("task_id")
    if not task_id:
        raise RuntimeError(f"Router did not return task_id: {data}")
    logger.info("Spawned task %s -> %s", task_id[:12], destination_agent_id)
    return task_id


# ---------------------------------------------------------------------------
# MCPBridge instance
# ---------------------------------------------------------------------------

_bridge = MCPBridge(
    router_url=ROUTER_URL,
    spawn_task=_spawn_task,
    tool_timeout=TOOL_TIMEOUT,
    exclude_agents=EXCLUDE_AGENTS,
)

# ---------------------------------------------------------------------------
# Agent polling
# ---------------------------------------------------------------------------

_poll_task: Optional[asyncio.Task] = None
_last_agent_snapshot: Optional[str] = None


async def _poll_agents() -> None:
    """Fetch ACL-filtered destinations from router and update bridge registry."""
    global _last_agent_snapshot

    if not _http_client or not _agent_id:
        logger.warning("Not connected to router — cannot poll agents")
        return

    try:
        r = await _http_client.get(f"{ROUTER_URL}/agent/destinations")
        r.raise_for_status()
        data = r.json()
        destinations = data.get("available_destinations", {})
    except Exception as exc:
        logger.error("Agent poll failed: %s", exc)
        return

    # Convert destinations dict to the list format update_agents expects.
    agents = [
        {"agent_id": aid, "agent_info": info}
        for aid, info in destinations.items()
    ]

    snapshot = json.dumps(
        sorted([a.get("agent_id", "") for a in agents]),
    )
    if snapshot == _last_agent_snapshot:
        return

    added, removed = _bridge.update_agents(agents)
    _last_agent_snapshot = snapshot

    if added:
        logger.info("Agents added: %s", ", ".join(added))
    if removed:
        logger.info("Agents removed: %s", ", ".join(removed))
    logger.info("MCP tools: %d agents exposed", _bridge.get_agent_count())


async def _poll_loop() -> None:
    while True:
        await _poll_agents()
        await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# MCP server launcher
# ---------------------------------------------------------------------------

_mcp_server_task: Optional[asyncio.Task] = None


async def _start_mcp_server() -> None:
    """Start the MCP transport server on MCP_PORT."""
    if MCP_TRANSPORT == "streamable-http":
        mcp_app = _bridge.build_streamable_http_app()
    else:
        mcp_app = _bridge.build_sse_app()

    config = uvicorn.Config(
        app=mcp_app,
        host=HOST,
        port=MCP_PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)
    logger.info("MCP %s server starting on %s:%d", MCP_TRANSPORT.upper(), HOST, MCP_PORT)
    await server.serve()


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global _poll_task, _mcp_server_task

    # Load persisted exclude list.
    cfg = _load_config()
    persisted_exclude = cfg.get("exclude_agents", [])
    if persisted_exclude:
        EXCLUDE_AGENTS.update(persisted_exclude)
        _bridge._exclude = EXCLUDE_AGENTS

    await _ensure_registered()
    await _poll_agents()  # Initial poll.

    _poll_task = asyncio.create_task(_poll_loop())
    _mcp_server_task = asyncio.create_task(_start_mcp_server())

    yield

    _poll_task.cancel()
    _mcp_server_task.cancel()
    try:
        await _poll_task
    except asyncio.CancelledError:
        pass
    try:
        await _mcp_server_task
    except asyncio.CancelledError:
        pass
    if _http_client:
        await _http_client.aclose()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Router-as-MCP-Server", lifespan=lifespan)

_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

add_config_routes(app, Path(__file__).resolve().parent, _require_auth, cookie_name="mcps_session")


# ---------------------------------------------------------------------------
# Router callback — result delivery
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "agent_id": _agent_id or "not initialized"}


@app.post("/refresh-info")
async def refresh_info(request: Request) -> JSONResponse:
    """Re-push AgentInfo and refresh agent registry."""
    if _auth_token:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not _secrets.compare_digest(auth[7:], _auth_token):
            return JSONResponse(status_code=403, content={"error": "Forbidden"})
    global _last_agent_snapshot
    if not _auth_token or not _agent_id:
        return JSONResponse({"status": "error", "detail": "Not connected to router."}, status_code=503)
    try:
        info = _mcp_server_agent_info.model_dump()
        info["agent_id"] = _agent_id  # Use registered ID (may differ from default)
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.put(
                f"{ROUTER_URL}/agent-info",
                headers={"Authorization": f"Bearer {_auth_token}"},
                json=info,
            )
            r.raise_for_status()
        # Force re-poll agents
        _last_agent_snapshot = None
        await _poll_agents()
        return JSONResponse({"status": "refreshed"})
    except Exception as exc:
        logger.warning("Failed to refresh agent info: %s", exc)
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=502)


@app.post("/receive")
async def receive(request: Request) -> JSONResponse:
    """Router delivers task results here."""
    # Verify delivery auth from router
    if _auth_token:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not _secrets.compare_digest(auth[7:], _auth_token):
            return JSONResponse(status_code=403, content={"error": "Forbidden"})

    data = await request.json()
    task_id = data.get("task_id", "")

    matched = _bridge.deliver_result(task_id, data)
    if matched:
        logger.info("Result delivered for task %s", task_id[:12])
    else:
        logger.warning("Received result for unknown task %s", task_id[:12])

    return JSONResponse(status_code=202, content={"status": "accepted"})


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


@app.post("/ui/change-password")
async def change_password(request: Request, mcps_session: Optional[str] = Cookie(default=None)) -> dict:
    _require_auth(mcps_session)
    body = await request.json()
    current = body.get("current_password", "")
    new_pw = body.get("new_password", "")
    if not new_pw or len(new_pw) < 4:
        raise HTTPException(status_code=400, detail="New password must be at least 4 characters")
    if not _admin_pw.verify(current):
        raise HTTPException(status_code=403, detail="Current password is incorrect")
    _admin_pw.change(new_pw)
    return {"status": "ok"}


@app.get("/ui/whoami")
async def whoami(mcps_session: Optional[str] = Cookie(default=None)) -> dict:
    return {"authenticated": bool(mcps_session and _verify_session_token(mcps_session))}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@app.get("/ui/status")
async def ui_status(mcps_session: Optional[str] = Cookie(default=None)) -> dict:
    _require_auth(mcps_session)
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
        "mcp_transport": MCP_TRANSPORT,
        "mcp_port": MCP_PORT,
        "mcp_url": f"http://{'localhost' if HOST == '0.0.0.0' else HOST}:{MCP_PORT}/sse" if MCP_TRANSPORT == "sse" else f"http://{'localhost' if HOST == '0.0.0.0' else HOST}:{MCP_PORT}/mcp",
        "poll_interval": POLL_INTERVAL,
        "tool_timeout": TOOL_TIMEOUT,
        "agent_count": _bridge.get_agent_count(),
    }


# ---------------------------------------------------------------------------
# Tools / agents
# ---------------------------------------------------------------------------


@app.get("/ui/tools")
async def ui_tools(mcps_session: Optional[str] = Cookie(default=None)) -> Any:
    _require_auth(mcps_session)
    return _bridge.get_tools_summary()


@app.get("/ui/agents")
async def ui_agents(mcps_session: Optional[str] = Cookie(default=None)) -> Any:
    _require_auth(mcps_session)
    if not _http_client or not _agent_id:
        return []
    try:
        r = await _http_client.get(f"{ROUTER_URL}/agent/destinations")
        r.raise_for_status()
        destinations = r.json().get("available_destinations", {})
        return [
            {"agent_id": aid, "agent_info": info}
            for aid, info in destinations.items()
        ]
    except Exception:
        return []


@app.post("/ui/agents/refresh")
async def ui_refresh_agents(mcps_session: Optional[str] = Cookie(default=None)) -> dict:
    _require_auth(mcps_session)
    global _last_agent_snapshot
    _last_agent_snapshot = None  # Force refresh.
    await _poll_agents()
    return {"status": "refreshed", "agent_count": _bridge.get_agent_count()}


# ---------------------------------------------------------------------------
# Exclude agents config
# ---------------------------------------------------------------------------


class ExcludeRequest(BaseModel):
    exclude_agents: list[str]


@app.get("/ui/config")
async def ui_config(mcps_session: Optional[str] = Cookie(default=None)) -> dict:
    _require_auth(mcps_session)
    return {
        "exclude_agents": sorted(EXCLUDE_AGENTS),
        "poll_interval": POLL_INTERVAL,
        "tool_timeout": TOOL_TIMEOUT,
    }


@app.post("/ui/config/exclude")
async def ui_update_exclude(
    body: ExcludeRequest,
    mcps_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(mcps_session)
    global _last_agent_snapshot
    EXCLUDE_AGENTS.clear()
    EXCLUDE_AGENTS.update(body.exclude_agents)
    _bridge._exclude = EXCLUDE_AGENTS

    # Persist.
    cfg = _load_config()
    cfg["exclude_agents"] = sorted(EXCLUDE_AGENTS)
    _save_config(cfg)

    # Force re-poll.
    _last_agent_snapshot = None
    await _poll_agents()

    return {"status": "updated", "exclude_agents": sorted(EXCLUDE_AGENTS)}


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------


@app.get("/ui/onboarding")
async def ui_onboarding(mcps_session: Optional[str] = Cookie(default=None)) -> dict:
    _require_auth(mcps_session)
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
    mcps_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(mcps_session)
    success = await _do_register(body.invitation_token)
    if success:
        return {"status": "registered", "agent_id": _agent_id}
    raise HTTPException(status_code=502, detail="Registration failed. Check logs.")


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


@app.get("/ui/logs")
async def ui_logs(mcps_session: Optional[str] = Cookie(default=None)) -> list[str]:
    _require_auth(mcps_session)
    return list(_log_ring)


# ---------------------------------------------------------------------------
# Root — serve SPA
# ---------------------------------------------------------------------------


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(str(_static_dir / "index.html"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=int(PORT), reload=False)
