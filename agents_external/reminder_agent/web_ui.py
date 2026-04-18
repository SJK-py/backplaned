"""
reminder_agent/web_ui.py — Management UI backend for the Reminder Agent.

Cookie-session auth pattern matching coding_agent / channel_agent.
Provides /ui/* endpoints for the SPA frontend.
"""

from __future__ import annotations

import json
import os
import secrets
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

# Shared config UI helper
from config_ui import add_config_routes


# ---------------------------------------------------------------------------
# Session auth
# ---------------------------------------------------------------------------

_SESSION_COOKIE = "reminder_session"
_SESSION_MAX_AGE = 3600 * 8  # 8 hours

_admin_pw: Optional[PasswordFile] = None


def _get_admin_pw() -> PasswordFile:
    global _admin_pw
    if _admin_pw is None:
        from agent import agent_config
        _admin_pw = PasswordFile(Path(agent_config.data_dir) / "admin_password.json", agent_config.admin_password)
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


def _require_auth(reminder_session: Optional[str]) -> None:
    if not _check_session(reminder_session):
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
    async def whoami(reminder_session: Optional[str] = Cookie(default=None)) -> dict:
        return {"authenticated": _check_session(reminder_session)}

    @router.post("/ui/change-password")
    async def change_password(request: Request, reminder_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(reminder_session)
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
    async def ui_status(reminder_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(reminder_session)
        from agent import agent_config, router_client, reminder_db, _checker_task
        import httpx as _httpx

        router_connected = False
        if router_client is not None:
            try:
                async with _httpx.AsyncClient(timeout=3.0) as _c:
                    r = await _c.get(f"{agent_config.router_url}/health")
                    router_connected = r.status_code == 200
            except Exception:
                pass

        user_ids = reminder_db.list_user_ids() if reminder_db else []

        return {
            "agent_id": agent_config.agent_id,
            "router_url": agent_config.router_url,
            "router_connected": router_connected,
            "llm_agent_id": agent_config.llm_agent_id,
            "endpoint_url": agent_config.agent_url or f"http://localhost:{agent_config.agent_port}",
            "check_interval": agent_config.check_interval,
            "event_notify_hours": agent_config.event_notify_hours,
            "task_notify_hours": agent_config.task_notify_hours,
            "urgent_task_notify_hours": agent_config.urgent_task_notify_hours,
            "core_agent_id": agent_config.core_agent_id,
            "checker_running": _checker_task is not None and not _checker_task.done(),
            "user_count": len(user_ids),
            "user_ids": user_ids,
        }

    # ------------------------------------------------------------------
    # Reminders — list users, browse per-user reminders
    # ------------------------------------------------------------------

    @router.get("/ui/users")
    async def list_users(reminder_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(reminder_session)
        from agent import reminder_db
        user_ids = reminder_db.list_user_ids()
        result = []
        for uid in user_ids:
            settings = await reminder_db.get_settings(uid)
            events = await reminder_db.get_all_events(uid)
            tasks = await reminder_db.get_all_tasks(uid)
            result.append({
                "user_id": uid,
                "event_count": len(events),
                "task_count": len(tasks),
                "timezone": settings.get("timezone", "UTC"),
                "model_id": settings.get("model_id"),
                "reporting_session_id": settings.get("reporting_session_id"),
                "reporting_agent_id": settings.get("reporting_agent_id"),
                "nighttime": f"{settings.get('nighttime_start', '?')}–{settings.get('nighttime_end', '?')}",
            })
        return {"users": result}

    @router.get("/ui/users/{user_id}/reminders")
    async def get_user_reminders(
        user_id: str,
        reminder_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        _require_auth(reminder_session)
        from agent import reminder_db
        db = await reminder_db.load(user_id)

        # Convert last_reminded from UTC to user timezone for display.
        settings = db.get("settings", {})
        tz_str = settings.get("timezone", "UTC")
        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime, timezone
            user_tz = ZoneInfo(tz_str)
        except Exception:
            user_tz = None

        def _localise_lr(item: dict) -> dict:
            lr = item.get("last_reminded")
            if lr and user_tz:
                try:
                    dt = datetime.fromisoformat(lr)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    item["last_reminded"] = dt.astimezone(user_tz).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass
            return item

        events = {k: _localise_lr(dict(v)) for k, v in db.get("events", {}).items()}
        tasks = {k: _localise_lr(dict(v)) for k, v in db.get("tasks", {}).items()}

        return {
            "user_id": user_id,
            "settings": settings,
            "events": events,
            "tasks": tasks,
        }

    @router.get("/ui/users/{user_id}/upcoming")
    async def get_upcoming_occurrences(
        user_id: str,
        days: int = 7,
        reminder_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        """Expand recurring events into individual occurrences for the next N days."""
        _require_auth(reminder_session)
        from agent import reminder_db
        from rrule_util import expand_event_occurrences
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        settings = await reminder_db.get_settings(user_id)
        tz_str = settings.get("timezone", "UTC")
        try:
            tz = ZoneInfo(tz_str)
        except Exception:
            tz = ZoneInfo("UTC")

        now = datetime.now(tz)
        window_end = now + timedelta(days=days)

        all_events = await reminder_db.get_all_events(user_id)
        occurrences: list[dict[str, Any]] = []
        for ev in all_events.values():
            if ev.get("status") == "cancelled":
                continue
            occs = expand_event_occurrences(ev, now, window_end)
            for occ in occs:
                occ.pop("_start_dt", None)
                occurrences.append(occ)

        occurrences.sort(key=lambda o: o.get("start", {}).get("dateTime", ""))
        return {"user_id": user_id, "days": days, "occurrences": occurrences}

    @router.put("/ui/users/{user_id}/settings")
    async def update_user_settings(
        user_id: str,
        request: Request,
        reminder_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        _require_auth(reminder_session)
        from agent import reminder_db
        body = await request.json()
        updates: dict[str, Any] = {}
        for key in ("nighttime_start", "nighttime_end", "timezone", "model_id", "reporting_session_id", "reporting_agent_id"):
            if key in body:
                updates[key] = body[key]
        if not updates:
            raise HTTPException(status_code=400, detail="No valid settings provided")
        settings = await reminder_db.update_settings(user_id, updates)
        return {"status": "ok", "settings": settings}

    @router.delete("/ui/users/{user_id}/events/{event_id}")
    async def delete_event(
        user_id: str,
        event_id: str,
        reminder_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        _require_auth(reminder_session)
        from agent import reminder_db
        ok = await reminder_db.remove_event(user_id, event_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Event not found")
        return {"status": "deleted", "event_id": event_id}

    @router.delete("/ui/users/{user_id}/tasks/{task_id}")
    async def delete_task(
        user_id: str,
        task_id: str,
        reminder_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        _require_auth(reminder_session)
        from agent import reminder_db
        ok = await reminder_db.remove_task(user_id, task_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Task not found")
        return {"status": "deleted", "task_id": task_id}

    # ------------------------------------------------------------------
    # Execution logs
    # ------------------------------------------------------------------

    @router.get("/ui/logs")
    async def list_logs(
        reminder_session: Optional[str] = Cookie(default=None),
        user_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict:
        _require_auth(reminder_session)
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
    async def get_log(task_id: str, reminder_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(reminder_session)
        from agent import agent_config
        lp = (Path(agent_config.log_dir) / f"{task_id}.json").resolve()
        if not str(lp).startswith(str(Path(agent_config.log_dir).resolve()) + os.sep):
            raise HTTPException(status_code=403, detail="Invalid task_id")
        if not lp.exists():
            raise HTTPException(status_code=404, detail="Log not found")
        return json.loads(lp.read_text())

    # ------------------------------------------------------------------
    # Refresh Agent Info
    # ------------------------------------------------------------------

    @router.post("/ui/refresh-info")
    async def ui_refresh_info(reminder_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(reminder_session)
        from agent import agent_config as cfg
        import httpx as _httpx
        try:
            _headers = {"Authorization": f"Bearer {cfg.agent_auth_token}"} if cfg.agent_auth_token else {}
            async with _httpx.AsyncClient(timeout=15.0) as c:
                r = await c.post(
                    f"http://localhost:{cfg.agent_port}/refresh-info",
                    headers=_headers,
                )
            if r.status_code < 300:
                return r.json()
            return {"status": "error", "detail": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    # ------------------------------------------------------------------
    # Onboarding
    # ------------------------------------------------------------------

    @router.get("/ui/onboarding")
    async def ui_onboarding(reminder_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(reminder_session)
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
            "endpoint_url": agent_config.agent_url or f"http://localhost:{agent_config.agent_port}",
            "registered": router_client is not None,
            "router_reachable": router_reachable,
            "available_destinations": list(available_destinations.keys()),
        }

    @router.post("/ui/onboarding/register")
    async def ui_register(request: Request, reminder_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_auth(reminder_session)
        body = await request.json()
        token = str(body.get("invitation_token", "")).strip()
        if not token:
            raise HTTPException(status_code=400, detail="invitation_token required")

        from agent import agent_config, available_destinations
        import agent as agent_mod
        from helper import RouterClient, onboard

        endpoint_url = agent_config.agent_url or f"http://localhost:{agent_config.agent_port}"

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
            creds_path = Path(agent_config.credentials_path)
            creds_path.write_text(json.dumps({"agent_id": resp.agent_id, "auth_token": resp.auth_token}))
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

    add_config_routes(router, Path(__file__).resolve().parent, _require_auth, cookie_name="reminder_session")

    return router
