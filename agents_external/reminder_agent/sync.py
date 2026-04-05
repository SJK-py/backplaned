"""
reminder_agent/sync.py — Google Calendar & Tasks sync (placeholder).

This module will implement bidirectional sync with Google Calendar API
and Google Tasks API.  Currently a stub with the interface defined.

Configuration per user (in reminders.json settings.google_sync):
    enabled:          bool   — whether sync is active
    calendar_api_key: str    — Google Calendar API OAuth token
    tasks_api_key:    str    — Google Tasks API OAuth token
    sync_interval:    int    — minutes between sync cycles
    conflict_rule:    str    — "local_wins" | "remote_wins" | "newest_wins"

References:
    - https://developers.google.com/workspace/calendar/api/guides/overview
    - https://developers.google.com/workspace/tasks/reference/rest
"""

from __future__ import annotations

import logging
from typing import Any

from db import ReminderDB

logger = logging.getLogger("reminder_agent.sync")


async def sync_user(user_id: str, db: ReminderDB) -> dict[str, Any]:
    """
    Synchronize a single user's reminders with Google Calendar & Tasks.

    Returns a summary dict with sync results.

    TODO: Implement OAuth2 flow, bidirectional sync, conflict resolution.
    """
    settings = await db.get_settings(user_id)
    google_sync = settings.get("google_sync", {})

    if not google_sync.get("enabled"):
        return {"status": "disabled", "user_id": user_id}

    logger.info("Google sync not yet implemented for user %s", user_id)
    return {"status": "not_implemented", "user_id": user_id}


async def sync_all_users(db: ReminderDB) -> list[dict[str, Any]]:
    """Run sync for all users that have Google sync enabled."""
    results = []
    for user_id in db.list_user_ids():
        result = await sync_user(user_id, db)
        results.append(result)
    return results
