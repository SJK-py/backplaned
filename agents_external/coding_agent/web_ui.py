"""
coding_agent/web_ui.py — Management UI backend for the Coding Agent.

Cookie-session auth pattern matching channel_agent.
Provides /ui/* endpoints for the SPA frontend.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import JSONResponse

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

import sys as _sys
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
from helper import PasswordFile


# ---------------------------------------------------------------------------
# Session auth
# ---------------------------------------------------------------------------

_SESSION_COOKIE = "coding_session"
_SESSION_MAX_AGE = 3600 * 8  # 8 hours

_admin_pw: Optional[PasswordFile] = None


def _get_admin_pw() -> PasswordFile:
    global _admin_pw
    if _admin_pw is None:
        from agent import agent_config
        data_dir = Path(agent_config.log_dir).parent
        _admin_pw = PasswordFile(data_dir / "admin_password.json", agent_config.admin_password)
    return _admin_pw


def _get_signer() -> URLSafeTimedSerializer:
    from agent import agent_config
    return URLSafeTimedSerializer(agent_config.session_secret)


def _check_session(token: Optional[str]) -> bool:
    if not token:
        return False
    try:
        _get_signer().loads(token, max_age=_SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _require_auth(coding_session: Optional[str]) -> None:
    if not _check_session(coding_session):
        raise HTTPException(status_code=401, detail="Not authenticated")


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def create_ui_router() -> APIRouter:
    """Create and return the management UI API router."""

    router = APIRouter(tags=["ui"])

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    @router.post("/ui/login")
    async def login(request: Request) -> JSONResponse:
        body = await request.json()
        pw = body.get("password", "")
        if not pw or not _get_admin_pw().verify(pw):
            raise HTTPException(status_code=403, detail="Invalid password")
        token = _get_signer().dumps("ok")
        resp = JSONResponse({"status": "ok"})
        resp.set_cookie(_SESSION_COOKIE, token, max_age=_SESSION_MAX_AGE, httponly=True, samesite="lax")
        return resp

    @router.post("/ui/logout")
    async def logout() -> JSONResponse:
        resp = JSONResponse({"status": "ok"})
        resp.delete_cookie(_SESSION_COOKIE)
        return resp

    @router.get("/ui/whoami")
    async def whoami(coding_session: Optional[str] = Cookie(default=None)) -> dict:
        return {"authenticated": _check_session(coding_session)}

    @router.post("/ui/change-password")
    async def change_password(request: Request, coding_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(coding_session)
        body = await request.json()
        current = body.get("current_password", "")
        new_pw = body.get("new_password", "")
        if not new_pw or len(new_pw) < 4:
            raise HTTPException(status_code=400, detail="New password must be at least 4 characters")
        apw = _get_admin_pw()
        if not apw.verify(current):
            raise HTTPException(status_code=403, detail="Current password is incorrect")
        apw.change(new_pw)
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @router.get("/ui/status")
    async def ui_status(coding_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(coding_session)
        from agent import agent_config, router_client, available_destinations, config_manager
        import httpx as _httpx
        router_connected = False
        if router_client is not None:
            try:
                async with _httpx.AsyncClient(timeout=3.0) as _c:
                    r = await _c.get(f"{agent_config.router_url}/health")
                    router_connected = r.status_code == 200
            except Exception:
                pass
        return {
            "agent_id": agent_config.agent_id,
            "router_url": agent_config.router_url,
            "router_connected": router_connected,
            "llm_agent_id": agent_config.llm_agent_id,
            "workspace_root": agent_config.workspace_root,
            "endpoint_url": agent_config.agent_endpoint_url or f"http://localhost:{agent_config.agent_port}",
            "available_destinations": list(available_destinations.keys()),
            "configured_users": config_manager.list_users(),
            "llm_timeout": agent_config.llm_timeout,
            "tool_timeout": agent_config.tool_timeout,
        }

    # ------------------------------------------------------------------
    # Users — list / create / get / update / delete
    # ------------------------------------------------------------------

    @router.get("/ui/users")
    async def list_users(coding_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(coding_session)
        from agent import config_manager
        users = config_manager.list_users()
        result: dict[str, Any] = {}
        for uid in users:
            result[uid] = config_manager.get_user_config(uid).model_dump()
        return {"users": result}

    @router.get("/ui/users/{user_id}")
    async def get_user(user_id: str, coding_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(coding_session)
        from agent import config_manager
        try:
            config = config_manager.get_user_config(user_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
        return {"user_id": user_id, "config": config.model_dump()}

    @router.post("/ui/users")
    async def create_user(request: Request, coding_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(coding_session)
        from agent import config_manager
        from config import UserConfig
        body = await request.json()
        user_id = str(body.get("user_id", "")).strip()
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id required")
        if user_id in config_manager.list_users():
            raise HTTPException(status_code=409, detail=f"User '{user_id}' already exists")
        config_manager.set_user_config(user_id, UserConfig())
        return {"status": "ok", "user_id": user_id}

    @router.put("/ui/users/{user_id}")
    async def update_user(user_id: str, request: Request, coding_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(coding_session)
        from agent import config_manager
        from config import UserConfig
        body = await request.json()
        try:
            current = config_manager.get_user_config(user_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
        merged = current.model_dump()
        # Only update provided fields
        for key in ["model_id", "limit_to_workspace", "allowed_paths", "blocked_commands",
                     "allow_all_commands", "allow_network", "max_iterations", "max_tool_calls"]:
            if key in body:
                merged[key] = body[key]
        config_manager.set_user_config(user_id, UserConfig.model_validate(merged))
        return {"status": "ok", "user_id": user_id, "config": merged}

    @router.delete("/ui/users/{user_id}")
    async def delete_user(
        user_id: str,
        request: Request,
        coding_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        _require_auth(coding_session)
        from agent import agent_config, config_manager
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        deleted = config_manager.delete_user(user_id)
        workspace_deleted = False
        if body.get("delete_workspace"):
            ws = (Path(agent_config.workspace_root) / user_id).resolve()
            ws_root = str(Path(agent_config.workspace_root).resolve())
            if not str(ws).startswith(ws_root + os.sep):
                raise HTTPException(status_code=403, detail="Invalid user_id")
            if ws.exists():
                shutil.rmtree(ws)
                workspace_deleted = True
        return {"status": "deleted" if deleted else "not_found", "workspace_deleted": workspace_deleted}

    # ------------------------------------------------------------------
    # Execution logs
    # ------------------------------------------------------------------

    @router.get("/ui/logs")
    async def list_logs(
        coding_session: Optional[str] = Cookie(default=None),
        user_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict:
        _require_auth(coding_session)
        from agent import agent_config
        log_dir = Path(agent_config.log_dir)
        if not log_dir.exists():
            return {"logs": []}
        logs: list[dict[str, Any]] = []
        for lf in sorted(log_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                entry = json.loads(lf.read_text())
                if user_id and entry.get("user_id") != user_id:
                    continue
                if status and entry.get("status") != status:
                    continue
                logs.append(entry)
                if len(logs) >= limit:
                    break
            except (json.JSONDecodeError, OSError):
                continue
        return {"logs": logs}

    @router.get("/ui/logs/{task_id}")
    async def get_log(task_id: str, coding_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(coding_session)
        from agent import agent_config
        lp = (Path(agent_config.log_dir) / f"{task_id}.json").resolve()
        if not str(lp).startswith(str(Path(agent_config.log_dir).resolve()) + os.sep):
            raise HTTPException(status_code=403, detail="Invalid task_id")
        if not lp.exists():
            raise HTTPException(status_code=404, detail="Log not found")
        return json.loads(lp.read_text())

    # ------------------------------------------------------------------
    # Workspace browser
    # ------------------------------------------------------------------

    @router.get("/ui/workspace/{user_id}")
    async def browse_workspace(
        user_id: str,
        path: str = ".",
        coding_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        _require_auth(coding_session)
        from agent import agent_config
        workspace = Path(agent_config.workspace_root) / user_id
        if not workspace.exists():
            raise HTTPException(status_code=404, detail="Workspace not found")
        target = (workspace / path).resolve()
        ws_resolved = workspace.resolve()
        if not (target == ws_resolved or str(target).startswith(str(ws_resolved) + os.sep)):
            raise HTTPException(status_code=403, detail="Path outside workspace")
        if not target.exists():
            raise HTTPException(status_code=404, detail="Path not found")
        if target.is_file():
            size = target.stat().st_size
            content = None
            if size <= 512_000:
                try:
                    content = target.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    content = "(binary file)"
            return {"type": "file", "path": path, "size": size, "content": content}
        entries: list[dict[str, Any]] = []
        for item in sorted(target.iterdir()):
            rel = str(item.relative_to(ws_resolved))
            e: dict[str, Any] = {"name": item.name, "path": rel, "type": "directory" if item.is_dir() else "file"}
            if item.is_file():
                e["size"] = item.stat().st_size
            entries.append(e)
        return {"type": "directory", "path": path, "entries": entries}

    # ------------------------------------------------------------------
    # Setup / Onboarding
    # ------------------------------------------------------------------

    @router.get("/ui/onboarding")
    async def ui_onboarding(coding_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(coding_session)
        from agent import agent_config, router_client, available_destinations
        import httpx as _httpx
        router_reachable = False
        if router_client is not None:
            try:
                async with _httpx.AsyncClient(timeout=3.0) as _c:
                    r = await _c.get(f"{agent_config.router_url}/health")
                    router_reachable = r.status_code == 200
            except Exception:
                pass
        return {
            "router_url": agent_config.router_url,
            "agent_id": agent_config.agent_id,
            "endpoint_url": agent_config.agent_endpoint_url or f"http://localhost:{agent_config.agent_port}",
            "registered": router_client is not None,
            "router_reachable": router_reachable,
            "available_destinations": list(available_destinations.keys()),
        }

    @router.post("/ui/onboarding/register")
    async def ui_register(request: Request, coding_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(coding_session)
        body = await request.json()
        token = str(body.get("invitation_token", "")).strip()
        if not token:
            raise HTTPException(status_code=400, detail="invitation_token required")

        from agent import agent_config, available_destinations
        import agent as agent_mod
        from helper import RouterClient, onboard

        endpoint_url = agent_config.agent_endpoint_url or f"http://localhost:{agent_config.agent_port}"
        try:
            resp = await onboard(
                router_url=agent_config.router_url,
                invitation_token=token,
                endpoint_url=f"{endpoint_url}/receive",
                agent_info=agent_mod.agent_info,
            )
            agent_config.agent_auth_token = resp.auth_token
            agent_config.agent_id = resp.agent_id
            agent_mod.available_destinations = resp.available_destinations
            # Save credentials
            creds_path = Path(agent_config.data_dir) / "credentials.json"
            creds_path.write_text(json.dumps({"agent_id": resp.agent_id, "auth_token": resp.auth_token}))
            # Replace router client
            if agent_mod.router_client:
                await agent_mod.router_client.aclose()
            agent_mod.router_client = RouterClient(
                router_url=agent_config.router_url,
                agent_id=resp.agent_id,
                auth_token=resp.auth_token,
            )
            return {"status": "ok", "agent_id": resp.agent_id}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    return router
