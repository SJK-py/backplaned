"""
cron_agent/web_ui.py — Admin web UI for the cron agent.
"""

from __future__ import annotations

import json
import secrets as _secrets
import sys as _sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from fastapi.responses import FileResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import sys as _sys
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
from config_ui import add_config_routes

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
from helper import PasswordFile


def build_web_router() -> APIRouter:
    router = APIRouter()

    COOKIE = "cron_session"
    MAX_AGE = 3600 * 8
    _signer_cache: list = []
    _admin_pw_cache: list[PasswordFile] = []

    def _get_signer() -> URLSafeTimedSerializer:
        if not _signer_cache:
            from agent import agent_config
            _signer_cache.append(
                URLSafeTimedSerializer(agent_config.session_secret or _secrets.token_hex(32))
            )
        return _signer_cache[0]

    def _get_admin_pw() -> PasswordFile:
        if not _admin_pw_cache:
            _admin_pw_cache.append(
                PasswordFile(Path(_cfg().data_dir) / "admin_password.json", _cfg().admin_password)
            )
        return _admin_pw_cache[0]

    def _cfg():
        from agent import agent_config
        return agent_config

    def _db():
        from agent import cron_db
        return cron_db

    def _make_token() -> str:
        return _get_signer().dumps("authenticated")

    def _verify(token: str) -> bool:
        try:
            _get_signer().loads(token, max_age=MAX_AGE)
            return True
        except (BadSignature, SignatureExpired):
            return False

    def _require_auth(cron_session: Optional[str]) -> None:
        if not cron_session or not _verify(cron_session):
            raise HTTPException(status_code=401, detail="Not authenticated")

    # -- Auth --

    @router.post("/ui/login")
    async def login(request: Request, response: Response) -> dict:
        body = await request.json()
        pw = body.get("password", "")
        if not pw or not _get_admin_pw().verify(pw):
            raise HTTPException(status_code=403, detail="Invalid password")
        token = _make_token()
        response.set_cookie(COOKIE, token, max_age=MAX_AGE, httponly=True, samesite="lax")
        return {"status": "ok"}

    @router.post("/ui/logout")
    async def logout(response: Response) -> dict:
        response.delete_cookie(COOKIE)
        return {"status": "ok"}

    @router.get("/ui/whoami")
    async def whoami(cron_session: Optional[str] = Cookie(default=None)) -> dict:
        return {"authenticated": bool(cron_session and _verify(cron_session))}

    @router.post("/ui/change-password")
    async def change_password(request: Request, cron_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(cron_session)
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

    # -- Status --

    @router.get("/ui/status")
    async def status(cron_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(cron_session)
        from agent import router_client, _scheduler_task
        cfg = _cfg()
        user_ids = _db().list_user_ids()
        return {
            "agent_id": cfg.agent_id,
            "router_url": cfg.router_url,
            "endpoint_url": cfg.endpoint_url,
            "router_connected": router_client is not None,
            "scheduler_running": _scheduler_task is not None and not _scheduler_task.done(),
            "check_interval": cfg.check_interval,
            "llm_agent_id": cfg.llm_agent_id,
            "core_agent_id": cfg.core_agent_id,
            "default_model_id": cfg.default_model_id or "(default)",
            "user_count": len(user_ids),
        }

    # -- Users & Jobs --

    @router.get("/ui/users")
    async def list_users(cron_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(cron_session)
        db = _db()
        user_ids = db.list_user_ids()
        result = []
        for uid in user_ids:
            settings = await db.get_settings(uid)
            jobs = await db.get_all_jobs(uid)
            enabled = sum(1 for j in jobs.values() if j.get("enabled", True))
            result.append({
                "user_id": uid,
                "total_jobs": len(jobs),
                "enabled_jobs": enabled,
                "model_id": settings.get("model_id") or "(default)",
                "reporting_agent_id": settings.get("reporting_agent_id"),
                "timezone": settings.get("timezone", "UTC"),
                "nighttime": f"{settings.get('nighttime_start', '?')}-{settings.get('nighttime_end', '?')}",
            })
        return {"users": result}

    @router.get("/ui/users/{user_id}/jobs")
    async def list_jobs(user_id: str, cron_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(cron_session)
        db = _db()
        jobs = await db.get_all_jobs(user_id)
        settings = await db.get_settings(user_id)
        return {
            "user_id": user_id,
            "settings": settings,
            "jobs": jobs,
            "note": "start_at/end_at are in user's local timezone. last_run/created_at are in UTC (frontend converts to local).",
        }

    @router.put("/ui/users/{user_id}/settings")
    async def update_settings(
        user_id: str, request: Request, cron_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        _require_auth(cron_session)
        body = await request.json()
        from db import _DEFAULT_SETTINGS
        updates = {k: body[k] for k in body if k in _DEFAULT_SETTINGS}
        if not updates:
            raise HTTPException(status_code=400, detail="No valid settings")
        settings = await _db().update_settings(user_id, updates)
        return {"status": "ok", "settings": settings}

    @router.put("/ui/users/{user_id}/jobs/{job_id}")
    async def modify_job(
        user_id: str, job_id: str, request: Request,
        cron_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        _require_auth(cron_session)
        body = await request.json()
        job = await _db().modify_job(user_id, job_id, body)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return {"status": "ok", "job": job}

    @router.delete("/ui/users/{user_id}/jobs/{job_id}")
    async def delete_job(
        user_id: str, job_id: str,
        cron_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        _require_auth(cron_session)
        removed = await _db().remove_job(user_id, job_id)
        if not removed:
            raise HTTPException(status_code=404, detail="Job not found")
        return {"status": "ok"}

    # -- Logs --

    @router.get("/ui/logs")
    async def get_logs(
        n: int = 50, cron_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        _require_auth(cron_session)
        log_dir = Path(_cfg().log_dir)
        logs: list[dict] = []
        if log_dir.exists():
            files = sorted(log_dir.glob("cron_*.json"), reverse=True)[:n]
            for f in files:
                try:
                    logs.append(json.loads(f.read_text()))
                except Exception:
                    pass
        return {"logs": logs}

    # -- Onboarding --

    @router.get("/ui/onboarding")
    async def onboarding(cron_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(cron_session)
        from agent import router_client, available_destinations
        import httpx
        cfg = _cfg()
        router_reachable = False
        if router_client:
            try:
                async with httpx.AsyncClient(timeout=3.0) as c:
                    r = await c.get(f"{cfg.router_url}/health")
                    router_reachable = r.status_code == 200
            except Exception:
                pass
        return {
            "router_url": cfg.router_url,
            "endpoint_url": cfg.endpoint_url,
            "agent_id": cfg.agent_id,
            "registered": router_client is not None,
            "router_reachable": router_reachable,
            "available_destinations": list(available_destinations.keys()) if available_destinations else [],
        }

    @router.post("/ui/onboarding/register")
    async def register(request: Request, cron_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(cron_session)
        body = await request.json()
        token = body.get("invitation_token", "").strip()
        if not token:
            raise HTTPException(status_code=400, detail="invitation_token required")

        cfg = _cfg()
        from helper import RouterClient, onboard as do_onboard
        import agent as agent_mod

        endpoint_url = cfg.agent_url or f"http://localhost:{cfg.agent_port}"
        try:
            resp = await do_onboard(
                router_url=cfg.router_url,
                invitation_token=token,
                endpoint_url=f"{endpoint_url}/receive",
                agent_info=agent_mod.agent_info,
            )
            cfg.agent_auth_token = resp.auth_token
            cfg.agent_id = resp.agent_id
            agent_mod.available_destinations = resp.available_destinations
            creds_path = Path(cfg.data_dir) / "credentials.json"
            creds_path.write_text(json.dumps({"agent_id": resp.agent_id, "auth_token": resp.auth_token}))
            if agent_mod.router_client:
                await agent_mod.router_client.aclose()
            agent_mod.router_client = RouterClient(
                router_url=cfg.router_url, agent_id=resp.agent_id, auth_token=resp.auth_token,
            )
            return {"status": "ok", "agent_id": resp.agent_id}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    # -- Refresh Agent Info --

    @router.post("/ui/refresh-info")
    async def ui_refresh_info(cron_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(cron_session)
        import httpx
        cfg = _cfg()
        try:
            _headers = {"Authorization": f"Bearer {cfg.agent_auth_token}"} if cfg.agent_auth_token else {}
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.post(f"http://localhost:{cfg.agent_port}/refresh-info", headers=_headers)
            if r.status_code < 300:
                return r.json()
            return {"status": "error", "detail": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    # -- Root --

    @router.get("/")
    async def root() -> FileResponse:
        return FileResponse(str(Path(__file__).parent / "static" / "index.html"))

    add_config_routes(router, Path(__file__).resolve().parent, _require_auth, cookie_name="cron_session")

    return router
