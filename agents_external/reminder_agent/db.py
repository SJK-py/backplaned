"""
reminder_agent/db.py — Per-user JSON database for reminders.

Google Calendar Event and Google Tasks compatible schema with local-only
extensions (last_reminded, urgent for tasks, google_id placeholder).

Each user gets ``data/users/{user_id}/reminders.json`` with three top-level
keys: ``settings``, ``events``, ``tasks``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("reminder_agent.db")

# ---------------------------------------------------------------------------
# Default structures
# ---------------------------------------------------------------------------

_DEFAULT_SETTINGS: dict[str, Any] = {
    "reporting_session_id": None,
    "reporting_agent_id": None,
    "nighttime_start": "22:00",
    "nighttime_end": "07:00",
    "timezone": "America/Los_Angeles",
    "model_id": None,
    "google_sync": {
        "enabled": False,
        "calendar_api_key": None,
        "tasks_api_key": None,
        "sync_interval": 15,
        "conflict_rule": "local_wins",
    },
}


def _empty_db() -> dict[str, Any]:
    import copy
    return {
        "settings": copy.deepcopy(_DEFAULT_SETTINGS),
        "events": {},
        "tasks": {},
    }


def _generate_id() -> str:
    return uuid.uuid4().hex[:8]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# ReminderDB — per-user database with asyncio lock
# ---------------------------------------------------------------------------


class ReminderDB:
    """Thread-safe (asyncio) per-user reminder database backed by JSON files."""

    def __init__(self, base_path: str | Path) -> None:
        self._base = Path(base_path)
        self._base.mkdir(parents=True, exist_ok=True)
        # Per-user locks to avoid concurrent writes to the same file.
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
        return self._user_dir(user_id) / "reminders.json"

    # -- Low-level I/O (called under lock) ----------------------------------

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

    # -- Public async API ---------------------------------------------------

    async def load(self, user_id: str) -> dict[str, Any]:
        async with self._lock_for(user_id):
            return self._load_raw(user_id)

    async def save(self, user_id: str, data: dict[str, Any]) -> None:
        async with self._lock_for(user_id):
            self._save_raw(user_id, data)

    async def get_settings(self, user_id: str) -> dict[str, Any]:
        db = await self.load(user_id)
        settings = db.get("settings") or {}
        # Merge with defaults for any missing keys.
        merged = copy.deepcopy(_DEFAULT_SETTINGS)
        merged.update(settings)
        return merged

    async def update_settings(self, user_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        async with self._lock_for(user_id):
            db = self._load_raw(user_id)
            settings = db.setdefault("settings", copy.deepcopy(_DEFAULT_SETTINGS))
            for k, v in updates.items():
                if k in _DEFAULT_SETTINGS:
                    settings[k] = v
            db["settings"] = settings
            self._save_raw(user_id, db)
            return settings

    async def ensure_reporting_session(
        self, user_id: str, session_id: str, agent_id: Optional[str] = None,
    ) -> None:
        """Set or refresh reporting_session_id and reporting_agent_id.

        Both fields are updated on every call so notifications always target
        the most recent caller.
        """
        async with self._lock_for(user_id):
            db = self._load_raw(user_id)
            settings = db.setdefault("settings", copy.deepcopy(_DEFAULT_SETTINGS))
            changed = False
            if settings.get("reporting_session_id") != session_id:
                settings["reporting_session_id"] = session_id
                changed = True
            if agent_id and settings.get("reporting_agent_id") != agent_id:
                settings["reporting_agent_id"] = agent_id
                changed = True
            if changed:
                self._save_raw(user_id, db)
                logger.info(
                    "Set reporting session=%s agent=%s for user %s",
                    settings["reporting_session_id"],
                    settings.get("reporting_agent_id"),
                    user_id,
                )

    # -- Events -------------------------------------------------------------

    async def add_event(self, user_id: str, event: dict[str, Any]) -> dict[str, Any]:
        eid = event.get("id") or _generate_id()
        now = _now_iso()
        record: dict[str, Any] = {
            "id": eid,
            "summary": event.get("summary", ""),
            "description": event.get("description", ""),
            "location": event.get("location", ""),
            "start": event.get("start", {}),
            "end": event.get("end", {}),
            "recurrence": event.get("recurrence", []),
            "status": event.get("status", "confirmed"),
            "reminders": event.get("reminders", {"useDefault": True, "overrides": []}),
            "last_reminded": None,
            "created_at": now,
            "updated_at": now,
            "google_id": None,
        }
        async with self._lock_for(user_id):
            db = self._load_raw(user_id)
            db.setdefault("events", {})[eid] = record
            self._save_raw(user_id, db)
        return record

    async def modify_event(self, user_id: str, event_id: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
        async with self._lock_for(user_id):
            db = self._load_raw(user_id)
            events = db.get("events", {})
            if event_id not in events:
                return None
            for k, v in updates.items():
                if k not in ("id", "created_at", "google_id"):
                    events[event_id][k] = v
            events[event_id]["updated_at"] = _now_iso()
            self._save_raw(user_id, db)
            return events[event_id]

    async def remove_event(self, user_id: str, event_id: str) -> bool:
        async with self._lock_for(user_id):
            db = self._load_raw(user_id)
            if event_id in db.get("events", {}):
                del db["events"][event_id]
                self._save_raw(user_id, db)
                return True
            return False

    # -- Tasks --------------------------------------------------------------

    async def add_task(self, user_id: str, task: dict[str, Any]) -> dict[str, Any]:
        tid = task.get("id") or _generate_id()
        now = _now_iso()
        record: dict[str, Any] = {
            "id": tid,
            "title": task.get("title", ""),
            "notes": task.get("notes", ""),
            "due": task.get("due"),
            "status": task.get("status", "needsAction"),
            "completed": None,
            "parent": task.get("parent"),
            "position": task.get("position", "0"),
            "last_reminded": None,
            "urgent": task.get("urgent", False),
            "created_at": now,
            "updated_at": now,
            "google_id": None,
        }
        async with self._lock_for(user_id):
            db = self._load_raw(user_id)
            db.setdefault("tasks", {})[tid] = record
            self._save_raw(user_id, db)
        return record

    async def modify_task(self, user_id: str, task_id: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
        async with self._lock_for(user_id):
            db = self._load_raw(user_id)
            tasks = db.get("tasks", {})
            if task_id not in tasks:
                return None
            for k, v in updates.items():
                if k not in ("id", "created_at", "google_id"):
                    tasks[task_id][k] = v
            tasks[task_id]["updated_at"] = _now_iso()
            self._save_raw(user_id, db)
            return tasks[task_id]

    async def complete_task(self, user_id: str, task_id: str) -> Optional[dict[str, Any]]:
        return await self.modify_task(user_id, task_id, {
            "status": "completed",
            "completed": _now_iso(),
        })

    async def remove_task(self, user_id: str, task_id: str) -> bool:
        async with self._lock_for(user_id):
            db = self._load_raw(user_id)
            if task_id in db.get("tasks", {}):
                del db["tasks"][task_id]
                self._save_raw(user_id, db)
                return True
            return False

    # -- Bulk helpers (used by checker) -------------------------------------

    async def get_all_events(self, user_id: str) -> dict[str, Any]:
        db = await self.load(user_id)
        return db.get("events", {})

    async def get_all_tasks(self, user_id: str) -> dict[str, Any]:
        db = await self.load(user_id)
        return db.get("tasks", {})

    async def update_last_reminded(self, user_id: str, item_ids: list[str]) -> None:
        """Atomically update last_reminded for a batch of item IDs (events or tasks)."""
        now = _now_iso()
        async with self._lock_for(user_id):
            db = self._load_raw(user_id)
            for iid in item_ids:
                if iid in db.get("events", {}):
                    db["events"][iid]["last_reminded"] = now
                elif iid in db.get("tasks", {}):
                    db["tasks"][iid]["last_reminded"] = now
            self._save_raw(user_id, db)

    def list_user_ids(self) -> list[str]:
        """Return all user_ids that have a reminders.json file."""
        result = []
        if not self._base.exists():
            return result
        for d in self._base.iterdir():
            if d.is_dir() and (d / "reminders.json").exists():
                result.append(d.name)
        return result
