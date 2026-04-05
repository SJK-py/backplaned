"""
cron_agent/db.py — Per-user cron job database backed by JSON files.

Each user gets ``data/users/{user_id}/cron_jobs.json``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("cron_agent.db")

_DEFAULT_SETTINGS: dict[str, Any] = {
    "reporting_session_id": None,
    "reporting_agent_id": None,
    "model_id": None,
    "nighttime_start": "22:00",
    "nighttime_end": "07:00",
    "timezone": "America/Los_Angeles",
}


def _empty_db() -> dict[str, Any]:
    return {"settings": dict(_DEFAULT_SETTINGS), "jobs": {}}


def _generate_id() -> str:
    return uuid.uuid4().hex[:8]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CronDB:
    """Thread-safe (asyncio) per-user cron job database."""

    def __init__(self, base_path: str | Path) -> None:
        self._base = Path(base_path)
        self._base.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, user_id: str) -> asyncio.Lock:
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    def _user_dir(self, user_id: str) -> Path:
        d = (self._base / user_id).resolve()
        if not str(d).startswith(str(self._base.resolve()) + os.sep) and d != self._base.resolve():
            raise ValueError(f"Invalid user_id: {user_id}")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _db_path(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "cron_jobs.json"

    def _load_raw(self, user_id: str) -> dict[str, Any]:
        p = self._db_path(user_id)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.warning("Corrupt DB for %s — resetting", user_id)
        return _empty_db()

    def _save_raw(self, user_id: str, data: dict[str, Any]) -> None:
        p = self._db_path(user_id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    # -- Settings --------------------------------------------------------------

    async def get_settings(self, user_id: str) -> dict[str, Any]:
        async with self._lock_for(user_id):
            db = self._load_raw(user_id)
        settings = db.get("settings") or {}
        merged = dict(_DEFAULT_SETTINGS)
        merged.update(settings)
        return merged

    async def update_settings(self, user_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        async with self._lock_for(user_id):
            db = self._load_raw(user_id)
            settings = db.setdefault("settings", dict(_DEFAULT_SETTINGS))
            for k, v in updates.items():
                if k in _DEFAULT_SETTINGS:
                    settings[k] = v
            db["settings"] = settings
            self._save_raw(user_id, db)
            return settings

    async def ensure_reporting_info(
        self, user_id: str, session_id: str, agent_id: Optional[str] = None,
    ) -> None:
        """Set or refresh reporting identifiers so notifications target the most recent caller."""
        async with self._lock_for(user_id):
            db = self._load_raw(user_id)
            settings = db.setdefault("settings", dict(_DEFAULT_SETTINGS))
            changed = False
            if settings.get("reporting_session_id") != session_id:
                settings["reporting_session_id"] = session_id
                changed = True
            if agent_id and settings.get("reporting_agent_id") != agent_id:
                settings["reporting_agent_id"] = agent_id
                changed = True
            if changed:
                self._save_raw(user_id, db)

    # -- Jobs ------------------------------------------------------------------

    async def add_job(self, user_id: str, job: dict[str, Any]) -> dict[str, Any]:
        job_id = job.get("id") or _generate_id()
        now = _now_iso()
        record: dict[str, Any] = {
            "id": job_id,
            "cron_expression": job.get("cron_expression", ""),
            "message": job.get("message", ""),
            "description": job.get("description", ""),
            "model_id": job.get("model_id"),
            "start_at": job.get("start_at"),
            "end_at": job.get("end_at"),
            "enabled": job.get("enabled", True),
            "last_run": None,
            "next_run": None,
            "run_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        async with self._lock_for(user_id):
            db = self._load_raw(user_id)
            db.setdefault("jobs", {})[job_id] = record
            self._save_raw(user_id, db)
        return record

    async def modify_job(
        self, user_id: str, job_id: str, updates: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        async with self._lock_for(user_id):
            db = self._load_raw(user_id)
            jobs = db.get("jobs", {})
            if job_id not in jobs:
                return None
            for k, v in updates.items():
                if k not in ("id", "created_at", "run_count"):
                    jobs[job_id][k] = v
            jobs[job_id]["updated_at"] = _now_iso()
            self._save_raw(user_id, db)
            return jobs[job_id]

    async def remove_job(self, user_id: str, job_id: str) -> bool:
        async with self._lock_for(user_id):
            db = self._load_raw(user_id)
            if job_id in db.get("jobs", {}):
                del db["jobs"][job_id]
                self._save_raw(user_id, db)
                return True
            return False

    async def get_job(self, user_id: str, job_id: str) -> Optional[dict[str, Any]]:
        async with self._lock_for(user_id):
            db = self._load_raw(user_id)
        return db.get("jobs", {}).get(job_id)

    async def get_all_jobs(self, user_id: str) -> dict[str, Any]:
        async with self._lock_for(user_id):
            db = self._load_raw(user_id)
        return db.get("jobs", {})

    async def mark_job_run(self, user_id: str, job_id: str) -> None:
        """Update last_run and increment run_count."""
        async with self._lock_for(user_id):
            db = self._load_raw(user_id)
            job = db.get("jobs", {}).get(job_id)
            if job:
                job["last_run"] = _now_iso()
                job["run_count"] = (job.get("run_count") or 0) + 1
                self._save_raw(user_id, db)

    def list_user_ids(self) -> list[str]:
        result = []
        if not self._base.exists():
            return result
        for d in self._base.iterdir():
            if d.is_dir() and (d / "cron_jobs.json").exists():
                result.append(d.name)
        return result
