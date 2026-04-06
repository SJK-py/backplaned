"""
web/main.py — Admin frontend for the Unified Router for Agents system.

Features:
  - Password-protected session cookie auth.
  - Proxy endpoints to the router's admin API.
  - Web-agent self-registration: registers itself with the router and receives
    task results via POST /agent/receive, enabling the Admin-Agent tab.
  - Long-poll endpoint for Admin-Agent tab results.

Run:
    cd web && uvicorn main:app --host 0.0.0.0 --port 8080 --reload

Environment (.env):
    HOST, PORT, ADMIN_PASSWORD, ROUTER_URL, ROUTER_ADMIN_TOKEN
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# Allow importing helper.py from the project root.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx
import secrets as _secrets
from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel

from helper import AgentInfo, OnboardResponse, PasswordFile, build_spawn_request, onboard

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOST: str = os.environ.get("AGENT_HOST", "0.0.0.0")
PORT: int = int(os.environ.get("AGENT_PORT", "8080"))
ADMIN_PASSWORD: str = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    import warnings as _w
    _w.warn("ADMIN_PASSWORD is not set — web UI login will be unavailable until configured", stacklevel=1)
ROUTER_URL: str = os.environ.get("ROUTER_URL", "http://localhost:8000").rstrip("/")
ROUTER_ADMIN_TOKEN: str = os.environ.get("ROUTER_ADMIN_TOKEN", "")
INVITATION_TOKEN: str = os.environ.get("INVITATION_TOKEN", "")

SECRET_KEY: str = os.environ.get("SESSION_SECRET", _secrets.token_hex(32))
SESSION_COOKIE = "ar_session"
SESSION_MAX_AGE = 3600 * 8  # 8 hours

_DATA_DIR = Path(__file__).parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
CREDENTIALS_FILE = _DATA_DIR / "credentials.json"
_admin_pw = PasswordFile(_DATA_DIR / "admin_password.json", ADMIN_PASSWORD)

WEB_AGENT_INFO = AgentInfo(
    agent_id="web_admin",
    description="Web admin UI agent. Spawns tasks for testing and receives results.",
    input_schema="llmdata: LLMData, payload: dict",
    output_schema="content: str",
    required_input=[],
    hidden=True,
)

# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

_signer = URLSafeTimedSerializer(SECRET_KEY)


def _make_session_token() -> str:
    return _signer.dumps("authenticated")


def _verify_session_token(token: str) -> bool:
    try:
        _signer.loads(token, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _require_auth(ar_session: Optional[str] = Cookie(default=None)) -> None:
    if not ar_session or not _verify_session_token(ar_session):
        raise HTTPException(status_code=401, detail="Not authenticated")


# ---------------------------------------------------------------------------
# Router admin HTTP client
# ---------------------------------------------------------------------------

_admin_headers = (
    {"Authorization": f"Bearer {ROUTER_ADMIN_TOKEN}"} if ROUTER_ADMIN_TOKEN else {}
)


async def _router_get(path: str, params: Optional[dict] = None) -> Any:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{ROUTER_URL}{path}", headers=_admin_headers, params=params)
    r.raise_for_status()
    return r.json()


async def _router_post(path: str, body: dict) -> Any:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(f"{ROUTER_URL}{path}", headers=_admin_headers, json=body)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Web-agent bridge (self-registration + result callback)
# ---------------------------------------------------------------------------

# task_id -> asyncio.Event (signalled when result arrives)
_pending_events: dict[str, asyncio.Event] = {}
# task_id -> result payload dict
_pending_results: dict[str, dict] = {}

# Web-agent credentials
_web_agent_id: Optional[str] = None
_web_agent_auth_token: Optional[str] = None
_web_agent_http_client: Optional[httpx.AsyncClient] = None


def _load_credentials() -> Optional[dict]:
    if CREDENTIALS_FILE.exists():
        try:
            return json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _save_credentials(agent_id: str, auth_token: str) -> None:
    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_FILE.write_text(
        json.dumps({"agent_id": agent_id, "auth_token": auth_token}),
        encoding="utf-8",
    )


async def _ensure_web_agent() -> None:
    """Register with the router using the standard invitation-token pattern."""
    global _web_agent_id, _web_agent_auth_token, _web_agent_http_client

    creds = _load_credentials()
    if creds:
        # Verify the agent still exists in the router
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(
                    f"{ROUTER_URL}/agent/destinations",
                    headers={"Authorization": f"Bearer {creds['auth_token']}"},
                )
                if r.status_code == 200:
                    _web_agent_id = creds["agent_id"]
                    _web_agent_auth_token = creds["auth_token"]
                    print(f"[web] Web-agent reused: {_web_agent_id}")
                    if _web_agent_http_client:
                        await _web_agent_http_client.aclose()
                    _web_agent_http_client = httpx.AsyncClient(
                        headers={"Authorization": f"Bearer {_web_agent_auth_token}"},
                        timeout=30.0,
                    )
                    return
                # 401/403 means credentials are invalid — fall through to re-onboard.
        except Exception as exc:
            # Router unreachable — trust saved credentials rather than attempting re-registration.
            print(f"[web] Could not verify web-agent credentials: {exc} — using saved credentials")
            _web_agent_id = creds["agent_id"]
            _web_agent_auth_token = creds["auth_token"]
            _web_agent_http_client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {_web_agent_auth_token}"},
                timeout=30.0,
            )
            return

    if not INVITATION_TOKEN:
        print("[web] INVITATION_TOKEN not set and no saved credentials — web-agent registration skipped.")
        return

    receive_url = f"http://localhost:{PORT}/agent/receive"
    try:
        resp: OnboardResponse = await onboard(
            router_url=ROUTER_URL,
            invitation_token=INVITATION_TOKEN,
            endpoint_url=receive_url,
            agent_info=WEB_AGENT_INFO,
        )
        _web_agent_id = resp.agent_id
        _web_agent_auth_token = resp.auth_token
        _save_credentials(_web_agent_id, _web_agent_auth_token)
        _web_agent_http_client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {_web_agent_auth_token}"},
            timeout=30.0,
        )
        print(f"[web] Web-agent registered: {_web_agent_id}")
    except Exception as exc:
        print(f"[web] Web-agent registration failed: {exc}")


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await _ensure_web_agent()
    yield
    if _web_agent_http_client:
        await _web_agent_http_client.aclose()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Agent Router Admin", lifespan=lifespan)

_static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


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
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return {"status": "ok"}


@app.post("/ui/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "ok"}


@app.get("/ui/whoami")
async def whoami(ar_session: Optional[str] = Cookie(default=None)) -> dict:
    return {"authenticated": bool(ar_session and _verify_session_token(ar_session))}


@app.post("/ui/change-password")
async def change_password(request: Request, ar_session: Optional[str] = Cookie(default=None)) -> dict:
    _require_auth(ar_session)
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
# Dashboard endpoints
# ---------------------------------------------------------------------------


@app.get("/ui/agents")
async def ui_agents(ar_session: Optional[str] = Cookie(default=None)) -> Any:
    _require_auth(ar_session)
    return await _router_get("/admin/agents")


@app.delete("/ui/agents/{agent_id}")
async def ui_disconnect_agent(
    agent_id: str,
    ar_session: Optional[str] = Cookie(default=None),
) -> Any:
    _require_auth(ar_session)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.delete(f"{ROUTER_URL}/admin/agents/{agent_id}", headers=_admin_headers)
    r.raise_for_status()
    return r.json()


@app.get("/ui/agents/{agent_id}/documentation")
async def ui_get_agent_documentation(
    agent_id: str,
    ar_session: Optional[str] = Cookie(default=None),
) -> dict:
    """Read the documentation file for an agent via the router admin API."""
    _require_auth(ar_session)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{ROUTER_URL}/admin/agents/{agent_id}/documentation",
            headers=_admin_headers,
        )
    r.raise_for_status()
    return r.json()


class UpdateDocumentationRequest(BaseModel):
    content: str


@app.put("/ui/agents/{agent_id}/documentation")
async def ui_update_agent_documentation(
    agent_id: str,
    body: UpdateDocumentationRequest,
    ar_session: Optional[str] = Cookie(default=None),
) -> dict:
    """Write documentation content for an agent via the router admin API."""
    _require_auth(ar_session)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.put(
            f"{ROUTER_URL}/admin/agents/{agent_id}/documentation",
            headers=_admin_headers,
            json={"content": body.content},
        )
    r.raise_for_status()
    return r.json()


@app.post("/ui/agents/{agent_id}/refresh-info")
async def ui_refresh_agent_info(
    agent_id: str,
    ar_session: Optional[str] = Cookie(default=None),
) -> dict:
    """Re-fetch agent documentation from its documentation_url via the router."""
    _require_auth(ar_session)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{ROUTER_URL}/admin/agents/{agent_id}/refresh-info",
            headers=_admin_headers,
        )
    r.raise_for_status()
    return r.json()


@app.delete("/ui/proxy-files/{file_key}")
async def ui_delete_proxy_file(
    file_key: str,
    ar_session: Optional[str] = Cookie(default=None),
) -> Any:
    _require_auth(ar_session)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.delete(f"{ROUTER_URL}/admin/proxy-files/{file_key}", headers=_admin_headers)
    r.raise_for_status()
    return r.json()


@app.delete("/ui/invitation-tokens/{token}")
async def ui_delete_invitation_token(
    token: str,
    ar_session: Optional[str] = Cookie(default=None),
) -> Any:
    _require_auth(ar_session)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.delete(f"{ROUTER_URL}/admin/invitation-tokens/{token}", headers=_admin_headers)
    r.raise_for_status()
    return r.json()


@app.delete("/ui/log")
async def ui_clear_log(ar_session: Optional[str] = Cookie(default=None)) -> Any:
    _require_auth(ar_session)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.delete(f"{ROUTER_URL}/admin/log", headers=_admin_headers)
    r.raise_for_status()
    return r.json()


@app.get("/ui/tasks")
async def ui_tasks(
    status: Optional[str] = None,
    agent_id: Optional[str] = None,
    limit: int = 100,
    ar_session: Optional[str] = Cookie(default=None),
) -> Any:
    _require_auth(ar_session)
    params: dict = {"limit": limit}
    if status:
        params["status"] = status
    if agent_id:
        params["agent_id"] = agent_id
    return await _router_get("/admin/tasks", params)


@app.get("/ui/proxy-files")
async def ui_proxy_files(ar_session: Optional[str] = Cookie(default=None)) -> Any:
    _require_auth(ar_session)
    return await _router_get("/admin/proxy-files")


# ---------------------------------------------------------------------------
# Onboarding endpoints
# ---------------------------------------------------------------------------


class InvitationCreateRequest(BaseModel):
    inbound_groups: list[str] = []
    outbound_groups: list[str] = []
    expires_in_hours: int = 24


@app.post("/ui/invitations")
async def ui_create_invitation(
    body: InvitationCreateRequest,
    ar_session: Optional[str] = Cookie(default=None),
) -> Any:
    _require_auth(ar_session)
    return await _router_post("/admin/invitation", body.model_dump())


@app.get("/ui/invitations")
async def ui_list_invitations(ar_session: Optional[str] = Cookie(default=None)) -> Any:
    _require_auth(ar_session)
    return await _router_get("/admin/invitations")


# ---------------------------------------------------------------------------
# ACL endpoints
# ---------------------------------------------------------------------------


class GroupAllowlistRequest(BaseModel):
    inbound_group: str
    outbound_group: str


class IndividualAllowlistRequest(BaseModel):
    agent_id: str
    destination_agent_id: str


class UpdateAgentGroupsRequest(BaseModel):
    inbound_groups: list[str]
    outbound_groups: list[str]


@app.get("/ui/group-allowlist")
async def ui_list_group_allowlist(ar_session: Optional[str] = Cookie(default=None)) -> Any:
    _require_auth(ar_session)
    return await _router_get("/admin/group-allowlist")


@app.post("/ui/group-allowlist")
async def ui_add_group_allowlist(
    body: GroupAllowlistRequest,
    ar_session: Optional[str] = Cookie(default=None),
) -> Any:
    _require_auth(ar_session)
    return await _router_post("/admin/group-allowlist", body.model_dump())


@app.delete("/ui/group-allowlist")
async def ui_delete_group_allowlist(
    body: GroupAllowlistRequest,
    ar_session: Optional[str] = Cookie(default=None),
) -> Any:
    _require_auth(ar_session)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.request(
            "DELETE",
            f"{ROUTER_URL}/admin/group-allowlist",
            headers=_admin_headers,
            json=body.model_dump(),
        )
    r.raise_for_status()
    return r.json()


@app.patch("/ui/agents/{agent_id}/groups")
async def ui_update_agent_groups(
    agent_id: str,
    body: UpdateAgentGroupsRequest,
    ar_session: Optional[str] = Cookie(default=None),
) -> Any:
    _require_auth(ar_session)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.patch(
            f"{ROUTER_URL}/admin/agents/{agent_id}/groups",
            headers=_admin_headers,
            json=body.model_dump(),
        )
    r.raise_for_status()
    return r.json()


@app.get("/ui/individual-allowlist")
async def ui_list_individual_allowlist(ar_session: Optional[str] = Cookie(default=None)) -> Any:
    _require_auth(ar_session)
    return await _router_get("/admin/individual-allowlist")


@app.post("/ui/individual-allowlist")
async def ui_add_individual_allowlist(
    body: IndividualAllowlistRequest,
    ar_session: Optional[str] = Cookie(default=None),
) -> Any:
    _require_auth(ar_session)
    return await _router_post("/admin/individual-allowlist", body.model_dump())


@app.delete("/ui/individual-allowlist")
async def ui_delete_individual_allowlist(
    body: IndividualAllowlistRequest,
    ar_session: Optional[str] = Cookie(default=None),
) -> Any:
    _require_auth(ar_session)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.request(
            "DELETE",
            f"{ROUTER_URL}/admin/individual-allowlist",
            headers=_admin_headers,
            json=body.model_dump(),
        )
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Embedded Agent Config endpoints
# ---------------------------------------------------------------------------


@app.get("/ui/agent-config/{agent_id}")
async def ui_get_agent_config(
    agent_id: str,
    ar_session: Optional[str] = Cookie(default=None),
) -> Any:
    _require_auth(ar_session)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{ROUTER_URL}/admin/agents/{agent_id}/config",
            headers=_admin_headers,
        )
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    return r.json()


@app.put("/ui/agent-config/{agent_id}")
async def ui_put_agent_config(
    agent_id: str,
    request: Request,
    ar_session: Optional[str] = Cookie(default=None),
) -> Any:
    _require_auth(ar_session)
    body = await request.json()
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.put(
            f"{ROUTER_URL}/admin/agents/{agent_id}/config",
            headers=_admin_headers,
            json={"config": body},
        )
    r.raise_for_status()
    return r.json()


@app.get("/ui/agent-config/{agent_id}/example")
async def ui_get_agent_config_example(
    agent_id: str,
    ar_session: Optional[str] = Cookie(default=None),
) -> Any:
    _require_auth(ar_session)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{ROUTER_URL}/admin/agents/{agent_id}/config-example",
            headers=_admin_headers,
        )
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Admin-Agent endpoints
# ---------------------------------------------------------------------------


class SendTaskRequest(BaseModel):
    target_agent_id: str
    payload: dict[str, Any] = {}


@app.post("/ui/send-task")
async def ui_send_task(
    body: SendTaskRequest,
    ar_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(ar_session)

    if not _web_agent_id or not _web_agent_http_client:
        raise HTTPException(
            status_code=503,
            detail="Web-agent is not registered. Check ROUTER_ADMIN_TOKEN in .env.",
        )

    spawn_payload = build_spawn_request(
        agent_id=_web_agent_id,
        identifier=None,
        parent_task_id=None,
        destination_agent_id=body.target_agent_id,
        payload=body.payload,
    )

    resp = await _web_agent_http_client.post(f"{ROUTER_URL}/route", json=spawn_payload)
    if resp.status_code not in (200, 202):
        raise HTTPException(
            status_code=502,
            detail=f"Router returned {resp.status_code}: {resp.text}",
        )

    task_id: str = resp.json().get("task_id", "")
    # Pre-register the event so the long-poll endpoint can wait on it.
    _pending_events[task_id] = asyncio.Event()
    return {"task_id": task_id}


@app.get("/ui/task-result/{task_id}")
async def ui_task_result(
    task_id: str,
    timeout: int = 60,
    ar_session: Optional[str] = Cookie(default=None),
) -> Any:
    _require_auth(ar_session)

    # If result already arrived, return immediately.
    if task_id in _pending_results:
        result = _pending_results.pop(task_id)
        _pending_events.pop(task_id, None)
        return result

    event = _pending_events.get(task_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Unknown task_id")

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        # Schedule deferred cleanup — allow late-arriving results a grace period.
        def _deferred_cleanup(tid: str = task_id) -> None:
            _pending_events.pop(tid, None)
            _pending_results.pop(tid, None)
        asyncio.get_running_loop().call_later(300, _deferred_cleanup)
        return {"status": "pending", "task_id": task_id}

    result = _pending_results.pop(task_id, {"status": "no_result", "task_id": task_id})
    _pending_events.pop(task_id, None)
    return result


# ---------------------------------------------------------------------------
# Log endpoints
# ---------------------------------------------------------------------------


@app.get("/ui/events/{task_id}")
async def ui_events(
    task_id: str,
    ar_session: Optional[str] = Cookie(default=None),
) -> Any:
    _require_auth(ar_session)
    return await _router_get(f"/admin/events/{task_id}")


# ---------------------------------------------------------------------------
# Web-agent callback (called by the router)
# ---------------------------------------------------------------------------


@app.post("/agent/refresh-info")
async def agent_refresh_info(request: Request) -> JSONResponse:
    """Re-push AgentInfo to the router (hidden admin agent)."""
    if _web_agent_auth_token:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not _secrets.compare_digest(auth[7:], _web_agent_auth_token):
            return JSONResponse(status_code=403, content={"error": "Forbidden"})
    if not _web_agent_id or not _web_agent_http_client:
        return JSONResponse({"status": "error", "detail": "Not connected."}, status_code=503)
    try:
        r = await _web_agent_http_client.put(
            f"{ROUTER_URL}/agent-info",
            json={
                "agent_id": _web_agent_id,
                "description": WEB_AGENT_INFO.description,
                "input_schema": WEB_AGENT_INFO.input_schema,
                "output_schema": WEB_AGENT_INFO.output_schema,
                "required_input": WEB_AGENT_INFO.required_input,
            },
        )
        r.raise_for_status()
        return JSONResponse({"status": "refreshed"})
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=502)


@app.post("/agent/receive")
async def agent_receive(request: Request) -> JSONResponse:
    """Router delivers task results here."""
    # Verify delivery auth from router
    if _web_agent_auth_token:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not _secrets.compare_digest(auth[7:], _web_agent_auth_token):
            return JSONResponse(status_code=403, content={"error": "Forbidden"})

    data = await request.json()
    task_id: str = data.get("task_id", "")

    _pending_results[task_id] = data
    event = _pending_events.get(task_id)
    if event:
        event.set()

    return JSONResponse(status_code=202, content={"status": "accepted"})


@app.get("/agent/health")
async def agent_health() -> dict:
    return {"status": "ok", "agent_id": _web_agent_id or "not initialized"}


# ---------------------------------------------------------------------------
# Root — serve the SPA
# ---------------------------------------------------------------------------


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(str(_static_dir / "index.html"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
