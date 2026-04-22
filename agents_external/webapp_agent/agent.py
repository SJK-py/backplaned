"""
webapp_agent/agent.py — User-facing web application agent.

Provides a modern web UI for chatting with core_personal_agent,
managing sessions, browsing files, and configuring user settings.

External agent — runs as a separate process, communicates over HTTP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx
from fastapi import FastAPI, Cookie, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from helper import (
    AgentInfo, AgentOutput, LLMData, RouterClient,
    build_result_request, onboard, PasswordFile,
)
from config import AgentConfig

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / "data" / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("webapp_agent")

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

agent_config: AgentConfig = None  # type: ignore
router_client: RouterClient = None  # type: ignore
available_destinations: dict[str, Any] = {}
_pending_results: dict[str, asyncio.Future] = {}
_pending_events: dict[str, asyncio.Event] = {}
_task_results: dict[str, dict[str, Any]] = {}

_AGENT_DIR = Path(__file__).resolve().parent
_USERS_DIR: Path = None  # type: ignore  # set in lifespan

# ---------------------------------------------------------------------------
# User auth helpers
# ---------------------------------------------------------------------------

def _user_dir(user_id: str) -> Path:
    d = _USERS_DIR / user_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_password_file(user_id: str) -> PasswordFile:
    return PasswordFile(_user_dir(user_id) / "password.json")


def _user_sessions_path(user_id: str) -> Path:
    return _user_dir(user_id) / "sessions.json"


def _load_user_sessions(user_id: str) -> list[dict[str, Any]]:
    p = _user_sessions_path(user_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_user_sessions(user_id: str, sessions: list[dict[str, Any]]) -> None:
    _user_sessions_path(user_id).write_text(
        json.dumps(sessions, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _user_inbox(user_id: str) -> Path:
    d = _user_dir(user_id) / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Per-session chat history (local to webapp_agent)
# ---------------------------------------------------------------------------

def _history_dir(user_id: str) -> Path:
    d = _user_dir(user_id) / "history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _history_path(user_id: str, session_id: str) -> Path:
    return _history_dir(user_id) / f"{session_id}.json"


def _load_chat_history(user_id: str, session_id: str) -> list[dict[str, Any]]:
    p = _history_path(user_id, session_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_chat_history(user_id: str, session_id: str, messages: list[dict[str, Any]]) -> None:
    _history_path(user_id, session_id).write_text(
        json.dumps(messages, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _append_chat(user_id: str, session_id: str, role: str, content: str) -> None:
    history = _load_chat_history(user_id, session_id)
    history.append({"role": role, "content": content})
    _save_chat_history(user_id, session_id, history)


def _delete_chat_history(user_id: str, session_id: str) -> None:
    p = _history_path(user_id, session_id)
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# Session auth (cookie-based)
# ---------------------------------------------------------------------------

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

_COOKIE = "webapp_session"
_MAX_AGE = 3600 * 24  # 24 hours


def _get_signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(agent_config.session_secret)


def _create_session_cookie(user_id: str) -> str:
    return _get_signer().dumps({"user_id": user_id})


def _validate_session(cookie: Optional[str]) -> str:
    if not cookie:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        data = _get_signer().loads(cookie, max_age=_MAX_AGE)
        return data["user_id"]
    except (BadSignature, SignatureExpired, KeyError):
        raise HTTPException(status_code=401, detail="Session expired")


# ---------------------------------------------------------------------------
# Token auth via channel_agent
# ---------------------------------------------------------------------------

_webapp_tokens: dict[str, str] = {}  # token → user_id (in-memory, set by channel_agent)


# ---------------------------------------------------------------------------
# Spawn to core and wait for result
# ---------------------------------------------------------------------------

async def _spawn_to_core(
    user_id: str,
    session_id: str,
    message: str,
    files: Optional[list[dict[str, Any]]] = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    if not router_client:
        raise HTTPException(status_code=503, detail="Not connected to router")

    identifier = f"wa_{uuid.uuid4().hex[:12]}"
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = loop.create_future()
    _pending_results[identifier] = fut

    payload: dict[str, Any] = {
        "user_id": user_id,
        "session_id": session_id,
        "message": message,
    }
    if files:
        payload["files"] = files

    core_agent = "core_personal_agent"
    try:
        await router_client.spawn(
            identifier=identifier,
            parent_task_id=None,
            destination_agent_id=core_agent,
            payload=payload,
        )
    except Exception as exc:
        _pending_results.pop(identifier, None)
        raise HTTPException(status_code=502, detail=f"Spawn failed: {exc}")

    try:
        result = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Request timed out")
    finally:
        _pending_results.pop(identifier, None)

    return result


async def _spawn_fire_and_forget(
    user_id: str,
    session_id: str,
    message: str,
) -> str:
    """Spawn to core without waiting for result. Returns task_id."""
    if not router_client:
        raise HTTPException(status_code=503, detail="Not connected to router")

    identifier = f"wa_noreply_{uuid.uuid4().hex[:8]}"
    payload = {"user_id": user_id, "session_id": session_id, "message": message}
    core_agent = "core_personal_agent"

    resp = await router_client.spawn(
        identifier=identifier,
        parent_task_id=None,
        destination_agent_id=core_agent,
        payload=payload,
    )
    return identifier


# ---------------------------------------------------------------------------
# Agent info
# ---------------------------------------------------------------------------

AGENT_GROUPS = (["channel"], ["channel"])

agent_info = AgentInfo(
    agent_id="webapp_agent",
    description="Web application agent. User-facing frontend for chat, session management, and file handling.",
    input_schema="user_id: str, session_id: str, message: str, files: Optional[List[ProxyFile]]",
    output_schema="content: str",
    required_input=["user_id", "session_id", "message"],
    hidden=True,
)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global agent_config, router_client, available_destinations, _USERS_DIR

    agent_config = AgentConfig.from_env()
    _USERS_DIR = Path(agent_config.data_dir) / "users"
    _USERS_DIR.mkdir(parents=True, exist_ok=True)

    creds_path = Path(agent_config.credentials_path)
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    saved_creds: dict[str, Any] = {}
    if creds_path.exists():
        try:
            saved_creds = json.loads(creds_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    endpoint_url = f"{agent_config.agent_url or f'http://localhost:{agent_config.agent_port}'}/receive"

    if saved_creds.get("auth_token"):
        agent_config.agent_auth_token = saved_creds["auth_token"]
        agent_config.agent_id = saved_creds.get("agent_id", agent_config.agent_id)
        router_client = RouterClient(
            router_url=agent_config.router_url,
            agent_id=agent_config.agent_id,
            auth_token=agent_config.agent_auth_token,
        )
        try:
            await router_client.refresh_from_agent_info(agent_info, endpoint_url=endpoint_url)
        except Exception as e:
            logger.warning("Failed to refresh agent info: %s", e)
        try:
            dest_data = await router_client.get_destinations()
            available_destinations = dest_data.get("available_destinations", {})
        except Exception as e:
            logger.warning("Failed to fetch destinations: %s", e)
        logger.info("Using saved credentials for '%s'.", agent_config.agent_id)

    elif agent_config.invitation_token:
        logger.info("Onboarding with router...")
        try:
            resp = await onboard(
                router_url=agent_config.router_url,
                invitation_token=agent_config.invitation_token,
                endpoint_url=endpoint_url,
                agent_info=agent_info,
            )
            agent_config.agent_auth_token = resp.auth_token
            agent_config.agent_id = resp.agent_id
            available_destinations = resp.available_destinations
            router_client = RouterClient(
                router_url=agent_config.router_url,
                agent_id=resp.agent_id,
                auth_token=resp.auth_token,
            )
            creds_path.write_text(json.dumps({
                "agent_id": resp.agent_id,
                "auth_token": resp.auth_token,
            }))
            logger.info("Onboarded as '%s'.", resp.agent_id)
        except Exception as e:
            logger.error("Onboarding failed: %s", e)
    else:
        logger.warning("No credentials or invitation token. Start without router.")

    logger.info("Webapp agent started on %s:%d", agent_config.agent_host, agent_config.agent_port)
    yield

    if router_client:
        await router_client.aclose()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Webapp Agent", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Router receive endpoint
# ---------------------------------------------------------------------------

@app.post("/receive")
async def receive_task(request: Request) -> JSONResponse:
    if agent_config and agent_config.agent_auth_token:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not secrets.compare_digest(
            auth[7:], agent_config.agent_auth_token
        ):
            return JSONResponse(status_code=403, content={"error": "Forbidden"})

    body = await request.json()

    # Handle result deliveries
    identifier = body.get("identifier")
    if body.get("destination_agent_id") is None and "status_code" in body:
        fut = _pending_results.get(identifier)
        if fut and not fut.done():
            fut.set_result(body)
        return JSONResponse({"status": "accepted"}, status_code=202)

    # Update destinations
    global available_destinations
    if "available_destinations" in body:
        available_destinations = body["available_destinations"]

    return JSONResponse({"status": "accepted"}, status_code=202)


@app.post("/refresh-info")
async def refresh_info(request: Request) -> JSONResponse:
    if router_client:
        endpoint_url = f"{agent_config.agent_url or f'http://localhost:{agent_config.agent_port}'}/receive"
        try:
            await router_client.refresh_from_agent_info(agent_info, endpoint_url=endpoint_url)
        except Exception:
            pass
    return JSONResponse({"status": "refreshed"})


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "agent_id": agent_config.agent_id if agent_config else "not initialized",
        "router_connected": router_client is not None,
    })


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@app.post("/api/login")
async def api_login(request: Request) -> JSONResponse:
    body = await request.json()
    user_id = str(body.get("user_id", "")).strip()
    password = str(body.get("password", "")).strip()
    if not user_id or not password:
        raise HTTPException(status_code=400, detail="user_id and password required")

    pw = _get_password_file(user_id)
    if not pw.verify(password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    cookie = _create_session_cookie(user_id)
    resp = JSONResponse({"status": "ok", "user_id": user_id})
    resp.set_cookie(_COOKIE, cookie, max_age=_MAX_AGE, httponly=True, samesite="lax")
    return resp


@app.post("/api/login-token")
async def api_login_token(request: Request) -> JSONResponse:
    body = await request.json()
    user_id = str(body.get("user_id", "")).strip()
    token = str(body.get("token", "")).strip()
    if not user_id or not token:
        raise HTTPException(status_code=400, detail="user_id and token required")

    # Validate token via channel_agent through the router.
    if not router_client:
        raise HTTPException(status_code=503, detail="Not connected to router")

    identifier = f"wa_auth_{uuid.uuid4().hex[:8]}"
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = loop.create_future()
    _pending_results[identifier] = fut

    try:
        await router_client.spawn(
            identifier=identifier,
            parent_task_id=None,
            destination_agent_id="channel_agent",
            payload={
                "user_id": user_id,
                "session_id": "SYSTEM",
                "message": f"<validate_webapp_token> {user_id} {token}",
            },
        )
        result = await asyncio.wait_for(fut, timeout=15.0)
    except asyncio.TimeoutError:
        _pending_results.pop(identifier, None)
        raise HTTPException(status_code=504, detail="Token validation timed out")
    except Exception as exc:
        _pending_results.pop(identifier, None)
        raise HTTPException(status_code=502, detail=f"Token validation error: {exc}")
    finally:
        _pending_results.pop(identifier, None)

    sc = result.get("status_code", 200)
    content = result.get("payload", {}).get("content", "")
    if sc and sc >= 400:
        raise HTTPException(status_code=401, detail=content or "Invalid or expired token")

    return JSONResponse({"status": "set_password", "user_id": user_id, "token": token})


@app.post("/api/set-password")
async def api_set_password(request: Request) -> JSONResponse:
    body = await request.json()
    user_id = str(body.get("user_id", "")).strip()
    password = str(body.get("password", "")).strip()
    if not user_id or not password:
        raise HTTPException(status_code=400, detail="user_id and password required")

    pw = _get_password_file(user_id)
    pw.change(password)

    cookie = _create_session_cookie(user_id)
    resp = JSONResponse({"status": "ok", "user_id": user_id})
    resp.set_cookie(_COOKIE, cookie, max_age=_MAX_AGE, httponly=True, samesite="lax")
    return resp


@app.post("/api/logout")
async def api_logout() -> JSONResponse:
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie(_COOKIE)
    return resp


@app.get("/api/me")
async def api_me(webapp_session: Optional[str] = Cookie(default=None)) -> JSONResponse:
    user_id = _validate_session(webapp_session)
    return JSONResponse({"user_id": user_id})


# ---------------------------------------------------------------------------
# Chat endpoints
# ---------------------------------------------------------------------------

@app.post("/api/chat")
async def api_chat(request: Request, webapp_session: Optional[str] = Cookie(default=None)) -> JSONResponse:
    user_id = _validate_session(webapp_session)
    body = await request.json()
    session_id = str(body.get("session_id", "")).strip()
    message = str(body.get("message", "")).strip()
    if not session_id or not message:
        raise HTTPException(status_code=400, detail="session_id and message required")

    _append_chat(user_id, session_id, "user", message)
    result = await _spawn_to_core(user_id, session_id, message)
    payload = result.get("payload", {})
    reply = payload.get("content", "")
    if reply:
        _append_chat(user_id, session_id, "assistant", reply)
    return JSONResponse({
        "content": reply,
        "files": payload.get("files"),
        "status_code": result.get("status_code", 200),
    })


@app.get("/api/sessions/{session_id}/history")
async def api_session_history(
    session_id: str,
    webapp_session: Optional[str] = Cookie(default=None),
) -> JSONResponse:
    user_id = _validate_session(webapp_session)
    messages = _load_chat_history(user_id, session_id)
    return JSONResponse(messages)


# ---------------------------------------------------------------------------
# Session management endpoints
# ---------------------------------------------------------------------------

@app.post("/api/sessions/new")
async def api_new_session(webapp_session: Optional[str] = Cookie(default=None)) -> JSONResponse:
    user_id = _validate_session(webapp_session)
    new_sid = f"wa_{uuid.uuid4().hex[:12]}"
    return JSONResponse({"session_id": new_sid})


@app.post("/api/sessions/{session_id}/archive")
async def api_archive_session(
    session_id: str,
    webapp_session: Optional[str] = Cookie(default=None),
) -> JSONResponse:
    user_id = _validate_session(webapp_session)
    result = await _spawn_to_core(user_id, session_id, "<new_session>", timeout=30.0)
    _delete_chat_history(user_id, session_id)
    return JSONResponse({"status": "archived", "content": result.get("payload", {}).get("content", "")})


@app.post("/api/sessions/{session_id}/default")
async def api_set_default(
    session_id: str,
    webapp_session: Optional[str] = Cookie(default=None),
) -> JSONResponse:
    user_id = _validate_session(webapp_session)
    result = await _spawn_to_core(user_id, session_id, "<set_default_session>", timeout=15.0)
    return JSONResponse({"content": result.get("payload", {}).get("content", "")})


@app.post("/api/sessions/{session_id}/rename")
async def api_rename_session(
    session_id: str,
    request: Request,
    webapp_session: Optional[str] = Cookie(default=None),
) -> JSONResponse:
    user_id = _validate_session(webapp_session)
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    result = await _spawn_to_core(user_id, session_id, f"<rename_session> {name}", timeout=15.0)
    return JSONResponse({"content": result.get("payload", {}).get("content", "")})


@app.get("/api/sessions/{session_id}/info")
async def api_session_info(
    session_id: str,
    webapp_session: Optional[str] = Cookie(default=None),
) -> JSONResponse:
    user_id = _validate_session(webapp_session)
    result = await _spawn_to_core(user_id, session_id, "<session_info>", timeout=15.0)
    content = result.get("payload", {}).get("content", "{}")
    try:
        return JSONResponse(json.loads(content))
    except Exception:
        return JSONResponse({"raw": content})


@app.get("/api/archived-sessions")
async def api_archived_sessions(webapp_session: Optional[str] = Cookie(default=None)) -> JSONResponse:
    user_id = _validate_session(webapp_session)
    # Use any active session (or a dummy) for the control token
    result = await _spawn_to_core(user_id, "SYSTEM", "<archived_sessions>", timeout=15.0)
    content = result.get("payload", {}).get("content", "[]")
    try:
        return JSONResponse(json.loads(content))
    except Exception:
        return JSONResponse([])


@app.post("/api/sessions/{session_id}/unarchive")
async def api_unarchive(
    session_id: str,
    webapp_session: Optional[str] = Cookie(default=None),
) -> JSONResponse:
    user_id = _validate_session(webapp_session)
    result = await _spawn_to_core(user_id, "SYSTEM", f"<unarchive_session> {session_id}", timeout=30.0)
    payload = result.get("payload", {})

    # Reconstruct local chat history from attached session history file.
    result_files = payload.get("files") or []
    for f in result_files:
        fname = f.get("original_filename") or Path(f.get("path", "")).name
        if fname == f"{session_id}.json":
            # Download the history file from the router and parse it.
            try:
                dl_url = f.get("path", "")
                key = f.get("key")
                protocol = f.get("protocol", "")
                if protocol == "router-proxy" and agent_config:
                    dl_url = f"{agent_config.router_url}{dl_url}"
                params = {"key": key} if key else {}
                async with httpx.AsyncClient(timeout=30.0) as client:
                    r = await client.get(dl_url, params=params)
                    r.raise_for_status()
                core_history = r.json()
                # Convert core history to webapp format (role + content).
                local_messages: list[dict[str, Any]] = []
                for entry in core_history:
                    role = entry.get("role", "")
                    content = entry.get("content", "")
                    if role in ("user", "assistant") and isinstance(content, str) and content:
                        local_messages.append({"role": role, "content": content})
                if local_messages:
                    _save_chat_history(user_id, session_id, local_messages)
                    logger.info("Reconstructed %d messages for unarchived session %s", len(local_messages), session_id)
            except Exception as exc:
                logger.warning("Failed to reconstruct history for %s: %s", session_id, exc)

    return JSONResponse({
        "content": payload.get("content", ""),
    })


@app.delete("/api/sessions/{session_id}")
async def api_delete_session(
    session_id: str,
    webapp_session: Optional[str] = Cookie(default=None),
) -> JSONResponse:
    user_id = _validate_session(webapp_session)
    result = await _spawn_to_core(user_id, "SYSTEM", f"<delete_session> {session_id}", timeout=15.0)
    return JSONResponse({"content": result.get("payload", {}).get("content", "")})


# ---------------------------------------------------------------------------
# Agent list endpoint
# ---------------------------------------------------------------------------

@app.get("/api/agents")
async def api_agents(webapp_session: Optional[str] = Cookie(default=None)) -> JSONResponse:
    user_id = _validate_session(webapp_session)
    result = await _spawn_to_core(user_id, "SYSTEM", "<agents_info>", timeout=15.0)
    content = result.get("payload", {}).get("content", "")
    return JSONResponse({"content": content})


# ---------------------------------------------------------------------------
# Link/unlink endpoints
# ---------------------------------------------------------------------------

@app.post("/api/sessions/{session_id}/link/{agent_id}")
async def api_link(
    session_id: str, agent_id: str,
    webapp_session: Optional[str] = Cookie(default=None),
) -> JSONResponse:
    user_id = _validate_session(webapp_session)
    result = await _spawn_to_core(user_id, session_id, f"<link_agent> {agent_id}", timeout=30.0)
    return JSONResponse({"content": result.get("payload", {}).get("content", "")})


@app.post("/api/sessions/{session_id}/unlink")
async def api_unlink(
    session_id: str,
    webapp_session: Optional[str] = Cookie(default=None),
) -> JSONResponse:
    user_id = _validate_session(webapp_session)
    result = await _spawn_to_core(user_id, session_id, "<unlink_agent>", timeout=30.0)
    return JSONResponse({"content": result.get("payload", {}).get("content", "")})


# ---------------------------------------------------------------------------
# User config endpoints
# ---------------------------------------------------------------------------

@app.get("/api/config")
async def api_get_config(
    webapp_session: Optional[str] = Cookie(default=None),
) -> JSONResponse:
    user_id = _validate_session(webapp_session)
    # Use SYSTEM session for config retrieval
    result = await _spawn_to_core(user_id, "SYSTEM", "<show_config>", timeout=15.0)
    return JSONResponse({"content": result.get("payload", {}).get("content", "")})


@app.post("/api/config")
async def api_set_config(
    request: Request,
    webapp_session: Optional[str] = Cookie(default=None),
) -> JSONResponse:
    user_id = _validate_session(webapp_session)
    body = await request.json()
    instruction = str(body.get("instruction", "")).strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="instruction required")
    result = await _spawn_to_core(user_id, "SYSTEM", f"<config_instruct> {instruction}", timeout=30.0)
    return JSONResponse({"content": result.get("payload", {}).get("content", "")})


# ---------------------------------------------------------------------------
# File inbox endpoints
# ---------------------------------------------------------------------------

@app.get("/api/inbox")
async def api_list_inbox(webapp_session: Optional[str] = Cookie(default=None)) -> JSONResponse:
    user_id = _validate_session(webapp_session)
    inbox = _user_inbox(user_id)
    files = []
    for p in sorted(inbox.rglob("*")):
        if p.is_file():
            files.append({
                "name": p.name,
                "size": p.stat().st_size,
                "modified": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
            })
    return JSONResponse(files)


def _safe_inbox_path(user_id: str, filename: str) -> Path:
    """Resolve filename within the user's inbox, blocking path traversal."""
    inbox = _user_inbox(user_id)
    fp = (inbox / filename).resolve()
    if not str(fp).startswith(str(inbox.resolve()) + os.sep) and fp != inbox.resolve():
        raise HTTPException(status_code=403, detail="Access denied")
    return fp


@app.get("/api/inbox/{filename}")
async def api_download_file(
    filename: str,
    webapp_session: Optional[str] = Cookie(default=None),
) -> FileResponse:
    user_id = _validate_session(webapp_session)
    fp = _safe_inbox_path(user_id, filename)
    if not fp.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(fp), filename=fp.name)


@app.delete("/api/inbox/{filename}")
async def api_delete_file(
    filename: str,
    webapp_session: Optional[str] = Cookie(default=None),
) -> JSONResponse:
    user_id = _validate_session(webapp_session)
    fp = _safe_inbox_path(user_id, filename)
    if not fp.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    fp.unlink()
    return JSONResponse({"status": "deleted"})


# ---------------------------------------------------------------------------
# App config endpoint (public, for frontend)
# ---------------------------------------------------------------------------

@app.get("/api/app-config")
async def api_app_config() -> JSONResponse:
    return JSONResponse({
        "archive_refresh_interval_sec": agent_config.archive_refresh_interval_sec if agent_config else 60,
        "agents_refresh_interval_sec": agent_config.agents_refresh_interval_sec if agent_config else 60,
        "session_title_delay_sec": agent_config.session_title_delay_sec if agent_config else 5,
    })


# ---------------------------------------------------------------------------
# Static files (SPA)
# ---------------------------------------------------------------------------

_static_dir = _AGENT_DIR / "static"
if _static_dir.exists():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    cfg = AgentConfig.from_env()
    uvicorn.run("agent:app", host=cfg.agent_host, port=cfg.agent_port)
