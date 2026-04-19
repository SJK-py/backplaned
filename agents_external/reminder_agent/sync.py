"""
reminder_agent/sync.py — Google Calendar & Tasks sync.

Bidirectional sync between ``ReminderDB`` and a user's Google account.
OAuth tokens live in ``settings.google_sync`` (populated by the OAuth
flow in ``web_ui.py``).  This module focuses on sync mechanics only.

Strategy
--------
* **Calendar**: incremental pull via ``events.list(syncToken=...)`` with
  full-resync fallback on HTTP 410.  Push inserts for local-only events
  (no ``google_id``) and patches for locally-modified events.
* **Tasks**: incremental pull via ``tasks.list(updatedMin=...)`` (Tasks
  API has no syncToken).  Push inserts/patches as above.
* **Conflicts**: per-user ``conflict_rule`` — local_wins / remote_wins /
  newest_wins (by ``updated_at``/``updated``).
* **Deletions**:
    - Remote deletions (``status=cancelled`` / ``deleted=true``) are
      applied locally by hard-delete.
    - Local hard-deletes do **not** currently propagate to Google; users
      should mark events ``status=cancelled`` or tasks ``status=completed``
      to have them reflect upstream.  (Tracked for follow-up.)

References
----------
- https://developers.google.com/workspace/calendar/api/guides/overview
- https://developers.google.com/workspace/calendar/api/guides/sync
- https://developers.google.com/workspace/tasks/reference/rest
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Any, Optional

from db import ReminderDB

logger = logging.getLogger("reminder_agent.sync")

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
    "openid",
    "email",
]
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Request execution with backoff
# ---------------------------------------------------------------------------

_RETRY_STATUSES = (429, 500, 502, 503, 504)


async def _execute(request, *, max_retries: int = 4) -> Any:
    """Run a googleapiclient request with exponential-backoff for 429/5xx.

    Other HttpError statuses (404, 410, 401, ...) propagate immediately so
    callers can react to them — this helper only retries the transient
    quota / availability errors that benefit from a wait-and-retry.
    """
    from googleapiclient.errors import HttpError

    for attempt in range(max_retries + 1):
        try:
            return await asyncio.to_thread(request.execute)
        except HttpError as e:
            status = getattr(e, "resp", None) and e.resp.status
            if status not in _RETRY_STATUSES or attempt >= max_retries:
                raise
            sleep_s = (2 ** attempt) + random.random()
            logger.info(
                "Google API %s — backing off %.1fs (attempt %d/%d)",
                status, sleep_s, attempt + 1, max_retries,
            )
            await asyncio.sleep(sleep_s)


def _build_credentials(google_sync: dict[str, Any], client_id: str, client_secret: str):
    """Construct google.oauth2.credentials.Credentials from stored tokens."""
    from google.oauth2.credentials import Credentials

    expiry = None
    exp_str = google_sync.get("token_expires_at")
    if exp_str:
        try:
            expiry = datetime.fromisoformat(exp_str)
            # google-auth expects a NAIVE UTC datetime for `expiry`.
            if expiry.tzinfo is not None:
                expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            expiry = None

    return Credentials(
        token=google_sync.get("access_token"),
        refresh_token=google_sync.get("refresh_token"),
        token_uri=GOOGLE_TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=GOOGLE_SCOPES,
        expiry=expiry,
    )


async def _refresh_if_needed(
    creds, db: ReminderDB, user_id: str,
) -> None:
    """Refresh creds if expired and persist the new access_token."""
    from google.auth.transport.requests import Request

    if creds.valid:
        return
    if not creds.refresh_token:
        raise RuntimeError("No refresh_token available — user must re-authorize.")

    def _do_refresh():
        creds.refresh(Request())

    await asyncio.to_thread(_do_refresh)

    expires_iso = None
    if creds.expiry is not None:
        expires_iso = creds.expiry.replace(tzinfo=timezone.utc).isoformat()
    await db.update_google_sync(user_id, {
        "access_token": creds.token,
        "token_expires_at": expires_iso,
    })


# ---------------------------------------------------------------------------
# Mapping: Google <-> local records
# ---------------------------------------------------------------------------

def _remote_event_to_local(remote: dict[str, Any]) -> dict[str, Any]:
    """Translate a Google Calendar event resource into our local shape."""
    rec = {
        "google_id": remote.get("id"),
        "etag": remote.get("etag"),
        "summary": remote.get("summary", ""),
        "description": remote.get("description", ""),
        "location": remote.get("location", ""),
        "start": remote.get("start", {}) or {},
        "end": remote.get("end", {}) or {},
        "recurrence": remote.get("recurrence", []) or [],
        "status": remote.get("status", "confirmed"),
        "reminders": remote.get("reminders", {"useDefault": True, "overrides": []}),
    }
    if remote.get("updated"):
        rec["updated_at"] = remote["updated"]
    return rec


def _format_exdate_for_master(
    original_start: dict[str, Any], master: dict[str, Any],
) -> Optional[str]:
    """Build an EXDATE line for ``original_start`` aligned with master's naive expansion.

    ``rrule_util.expand_event_occurrences`` strips tz from dtstart and iterates
    in naive form, so EXDATE values must also be naive in the master's local
    timezone to match.  Date-only all-day events use ``VALUE=DATE``.
    """
    if not original_start:
        return None

    # All-day instance ("date" present instead of "dateTime").
    if original_start.get("date"):
        try:
            d = datetime.fromisoformat(original_start["date"])
            return f"EXDATE;VALUE=DATE:{d.strftime('%Y%m%d')}"
        except ValueError:
            return None

    dt_str = original_start.get("dateTime")
    if not dt_str:
        return None
    try:
        exc_dt = datetime.fromisoformat(dt_str)
    except ValueError:
        return None

    master_tz_name = (master.get("start") or {}).get("timeZone")
    if master_tz_name:
        try:
            from zoneinfo import ZoneInfo
            master_tz = ZoneInfo(master_tz_name)
            if exc_dt.tzinfo is not None:
                exc_dt = exc_dt.astimezone(master_tz)
        except Exception:
            pass
    if exc_dt.tzinfo is not None:
        exc_dt = exc_dt.replace(tzinfo=None)
    return f"EXDATE:{exc_dt.strftime('%Y%m%dT%H%M%S')}"


async def _apply_exception(
    db: ReminderDB, user_id: str, item: dict[str, Any],
) -> str:
    """Handle a pulled recurrence-exception item.

    Returns one of: "cancelled", "modified", "orphan", "noop".
    """
    master_gid = item.get("recurringEventId")
    original_start = item.get("originalStartTime") or {}
    if not master_gid:
        return "noop"

    match = await db.find_event_by_google_id(user_id, master_gid)
    if match is None:
        # Master not synced yet (e.g. exception arrived before its master
        # in the same batch, or master is filtered out) — drop silently.
        return "orphan"
    _master_id, master = match

    exdate = _format_exdate_for_master(original_start, master)
    if exdate:
        await db.apply_master_exdate(user_id, master_gid, exdate)

    status = item.get("status", "confirmed")
    if status == "cancelled":
        return "cancelled"

    # Modified instance: store as a standalone one-off event so the agenda
    # shows it at its new time. It has its own google_id and no recurrence.
    rec = _remote_event_to_local(item)
    rec["recurrence"] = []
    await db.apply_remote_event(user_id, rec)
    return "modified"


def _local_event_to_remote(local: dict[str, Any]) -> dict[str, Any]:
    """Translate a local event record into a Google Calendar event body."""
    body: dict[str, Any] = {
        "summary": local.get("summary", ""),
    }
    if local.get("description"):
        body["description"] = local["description"]
    if local.get("location"):
        body["location"] = local["location"]
    if local.get("start"):
        body["start"] = local["start"]
    if local.get("end"):
        body["end"] = local["end"]
    if local.get("recurrence"):
        body["recurrence"] = local["recurrence"]
    if local.get("status"):
        body["status"] = local["status"]
    if local.get("reminders"):
        body["reminders"] = local["reminders"]
    return body


def _remote_task_to_local(remote: dict[str, Any]) -> dict[str, Any]:
    """Translate a Google Tasks resource into our local shape."""
    rec: dict[str, Any] = {
        "google_id": remote.get("id"),
        "etag": remote.get("etag"),
        "title": remote.get("title", ""),
        "notes": remote.get("notes", ""),
        "due": remote.get("due"),
        "status": remote.get("status", "needsAction"),
        "completed": remote.get("completed"),
        "parent": remote.get("parent"),
        "position": remote.get("position", "0"),
    }
    if remote.get("updated"):
        rec["updated_at"] = remote["updated"]
    return rec


def _local_task_to_remote(local: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": local.get("title", ""),
    }
    if local.get("notes"):
        body["notes"] = local["notes"]
    if local.get("due"):
        body["due"] = local["due"]
    if local.get("status"):
        body["status"] = local["status"]
    return body


# ---------------------------------------------------------------------------
# Conflict resolution
# ---------------------------------------------------------------------------

def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    """Parse an ISO8601 timestamp (accepts Google's trailing 'Z') to aware UTC."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _prefer_remote(local: dict[str, Any], remote_updated: Optional[str], rule: str) -> bool:
    """Return True when the remote version should overwrite local."""
    if rule == "remote_wins":
        return True
    if rule == "local_wins":
        return False
    # newest_wins: parse both sides as datetimes — Google emits
    # "...789Z" while our _now_iso emits "...789012+00:00", so a naive
    # string compare wrongly favors remote because 'Z' > '0' lexically.
    local_dt = _parse_ts(local.get("updated_at"))
    remote_dt = _parse_ts(remote_updated)
    if remote_dt is None:
        return False
    if local_dt is None:
        return True
    return remote_dt >= local_dt


# ---------------------------------------------------------------------------
# Calendar sync
# ---------------------------------------------------------------------------

async def _calendar_sync(
    service, db: ReminderDB, user_id: str, google_sync: dict[str, Any],
) -> dict[str, Any]:
    calendar_id = google_sync.get("calendar_id") or "primary"
    sync_token = google_sync.get("calendar_sync_token")
    conflict_rule = google_sync.get("conflict_rule") or "newest_wins"

    from googleapiclient.errors import HttpError

    # --- TOMBSTONES: push local deletions first ---------------------------
    tombstones_pushed = 0
    pending = await db.get_pending_deletes(user_id)
    for gid in pending.get("events", []):
        try:
            await _execute(service.events().delete(
                calendarId=calendar_id, eventId=gid,
            ))
            await db.clear_pending_delete(user_id, "events", gid)
            tombstones_pushed += 1
        except HttpError as e:
            status = getattr(e, "resp", None) and e.resp.status
            if status in (404, 410):
                # Already gone on Google's side — clear locally too.
                await db.clear_pending_delete(user_id, "events", gid)
                tombstones_pushed += 1
            else:
                logger.warning(
                    "Calendar tombstone push failed for %s/%s: %s", user_id, gid, e,
                )

    # --- PULL: fetch remote deltas ----------------------------------------
    applied = 0
    deleted = 0

    def _list_first(**kwargs):
        return service.events().list(calendarId=calendar_id, **kwargs).execute()

    def _list_next(prev_request, prev_response):
        return service.events().list_next(prev_request, prev_response)

    new_sync_token = sync_token
    cancellations_merged = 0
    modifications_stored = 0
    orphans_dropped = 0
    pending_delete_ids = set(pending.get("events", []))

    # Pull attempt(s): start with the stored syncToken; on 410, clear it
    # and immediately run a full resync in the same cycle rather than
    # waiting for the next tick.
    attempts = [sync_token, None] if sync_token else [None]
    for attempt_token in attempts:
        if attempt_token:
            request = service.events().list(
                calendarId=calendar_id, syncToken=attempt_token, showDeleted=True,
            )
        else:
            # Full fetch — singleEvents=False keeps recurrence rules intact.
            request = service.events().list(
                calendarId=calendar_id, showDeleted=True, singleEvents=False, maxResults=250,
            )
        try:
            while request is not None:
                response = await _execute(request)
                for item in response.get("items", []):
                    gid = item.get("id")
                    if not gid:
                        continue
                    if item.get("recurringEventId"):
                        outcome = await _apply_exception(db, user_id, item)
                        if outcome == "cancelled":
                            cancellations_merged += 1
                        elif outcome == "modified":
                            modifications_stored += 1
                        elif outcome == "orphan":
                            orphans_dropped += 1
                        continue
                    if item.get("status") == "cancelled":
                        if await db.delete_event_by_google_id(user_id, gid):
                            deleted += 1
                        continue
                    # Don't resurrect something we're in the middle of deleting.
                    if gid in pending_delete_ids:
                        continue
                    match = await db.find_event_by_google_id(user_id, gid)
                    if match is not None:
                        _, local = match
                        if db.is_dirty(local) and not _prefer_remote(
                            local, item.get("updated"), conflict_rule,
                        ):
                            continue
                    await db.apply_remote_event(user_id, _remote_event_to_local(item))
                    applied += 1
                new_sync_token = response.get("nextSyncToken") or new_sync_token
                request = await asyncio.to_thread(_list_next, request, response)
            break  # success — don't fall through to the retry attempt
        except HttpError as e:
            status = getattr(e, "resp", None) and e.resp.status
            if status == 410 and attempt_token is not None:
                logger.warning(
                    "Calendar syncToken expired for user %s; falling back to full resync.",
                    user_id,
                )
                new_sync_token = None
                await db.update_google_sync(user_id, {"calendar_sync_token": None})
                continue  # retry with attempt_token=None
            raise

    if new_sync_token:
        await db.update_google_sync(user_id, {"calendar_sync_token": new_sync_token})
    if orphans_dropped:
        logger.info(
            "Dropped %d orphan exception(s) for user %s (master not local yet)",
            orphans_dropped, user_id,
        )

    # --- PUSH: local changes --------------------------------------------
    pushed_created = 0
    pushed_updated = 0
    push_conflicts = 0
    events = await db.get_all_events(user_id)
    for eid, ev in events.items():
        if not db.is_dirty(ev):
            continue
        body = _local_event_to_remote(ev)
        try:
            if ev.get("google_id"):
                req = service.events().patch(
                    calendarId=calendar_id, eventId=ev["google_id"], body=body,
                )
                if ev.get("etag"):
                    req.headers["If-Match"] = ev["etag"]
                created = await _execute(req)
                await db.set_event_sync_metadata(
                    user_id, eid, etag=created.get("etag"), mark_synced=True,
                )
                pushed_updated += 1
            else:
                created = await _execute(service.events().insert(
                    calendarId=calendar_id, body=body,
                ))
                await db.set_event_sync_metadata(
                    user_id, eid,
                    google_id=created.get("id"),
                    etag=created.get("etag"),
                    mark_synced=True,
                )
                pushed_created += 1
        except HttpError as e:
            status = getattr(e, "resp", None) and e.resp.status
            if status == 412:
                push_conflicts += 1
                logger.info(
                    "Calendar 412 for %s/%s — remote moved; deferring to next pull",
                    user_id, eid,
                )
            else:
                logger.warning("Calendar push failed for %s/%s: %s", user_id, eid, e)

    return {
        "calendar_applied": applied,
        "calendar_deleted": deleted,
        "calendar_tombstones_pushed": tombstones_pushed,
        "calendar_pushed_created": pushed_created,
        "calendar_pushed_updated": pushed_updated,
        "calendar_push_conflicts": push_conflicts,
        "calendar_cancellations_merged": cancellations_merged,
        "calendar_modifications_stored": modifications_stored,
        "calendar_orphans_dropped": orphans_dropped,
    }


# ---------------------------------------------------------------------------
# Tasks sync
# ---------------------------------------------------------------------------

async def _tasks_sync(
    service, db: ReminderDB, user_id: str, google_sync: dict[str, Any],
) -> dict[str, Any]:
    tasklist_id = google_sync.get("tasklist_id") or "@default"
    updated_min = google_sync.get("tasks_updated_min")
    conflict_rule = google_sync.get("conflict_rule") or "newest_wins"

    from googleapiclient.errors import HttpError

    applied = 0
    deleted = 0

    # --- TOMBSTONES: push local deletions first ---------------------------
    tombstones_pushed = 0
    pending = await db.get_pending_deletes(user_id)
    for gid in pending.get("tasks", []):
        try:
            await _execute(service.tasks().delete(
                tasklist=tasklist_id, task=gid,
            ))
            await db.clear_pending_delete(user_id, "tasks", gid)
            tombstones_pushed += 1
        except HttpError as e:
            status = getattr(e, "resp", None) and e.resp.status
            if status in (404, 410):
                await db.clear_pending_delete(user_id, "tasks", gid)
                tombstones_pushed += 1
            else:
                logger.warning(
                    "Tasks tombstone push failed for %s/%s: %s", user_id, gid, e,
                )

    # Record "now" before we start — use as the new high-watermark after success.
    poll_started_at = _now_iso()

    # --- PULL ----------------------------------------------------------
    kwargs: dict[str, Any] = {
        "tasklist": tasklist_id,
        "showHidden": True,
        "showDeleted": True,
        "showCompleted": True,
        "maxResults": 100,
    }
    if updated_min:
        kwargs["updatedMin"] = updated_min

    try:
        request = service.tasks().list(**kwargs)
        while request is not None:
            response = await _execute(request)
            for item in response.get("items", []):
                gid = item.get("id")
                if not gid:
                    continue
                if item.get("deleted"):
                    if await db.delete_task_by_google_id(user_id, gid):
                        deleted += 1
                    continue
                match = await db.find_task_by_google_id(user_id, gid)
                if match is not None:
                    _, local = match
                    if db.is_dirty(local) and not _prefer_remote(
                        local, item.get("updated"), conflict_rule,
                    ):
                        continue
                await db.apply_remote_task(user_id, _remote_task_to_local(item))
                applied += 1
            request = await asyncio.to_thread(
                lambda r=request, resp=response: service.tasks().list_next(r, resp)
            )
    except HttpError as e:
        logger.warning("Tasks pull failed for user %s: %s", user_id, e)
        raise

    # --- PUSH ----------------------------------------------------------
    pushed_created = 0
    pushed_updated = 0
    push_conflicts = 0
    tasks = await db.get_all_tasks(user_id)
    for tid, tk in tasks.items():
        if not db.is_dirty(tk):
            continue
        body = _local_task_to_remote(tk)
        try:
            if tk.get("google_id"):
                req = service.tasks().patch(
                    tasklist=tasklist_id, task=tk["google_id"], body=body,
                )
                if tk.get("etag"):
                    req.headers["If-Match"] = tk["etag"]
                created = await _execute(req)
                await db.set_task_sync_metadata(
                    user_id, tid, etag=created.get("etag"), mark_synced=True,
                )
                pushed_updated += 1
            else:
                created = await _execute(service.tasks().insert(
                    tasklist=tasklist_id, body=body,
                ))
                await db.set_task_sync_metadata(
                    user_id, tid,
                    google_id=created.get("id"),
                    etag=created.get("etag"),
                    mark_synced=True,
                )
                pushed_created += 1
        except HttpError as e:
            status = getattr(e, "resp", None) and e.resp.status
            if status == 412:
                push_conflicts += 1
                logger.info(
                    "Tasks 412 for %s/%s — remote moved; deferring to next pull",
                    user_id, tid,
                )
            else:
                logger.warning("Tasks push failed for %s/%s: %s", user_id, tid, e)

    await db.update_google_sync(user_id, {"tasks_updated_min": poll_started_at})

    return {
        "tasks_applied": applied,
        "tasks_deleted": deleted,
        "tasks_tombstones_pushed": tombstones_pushed,
        "tasks_pushed_created": pushed_created,
        "tasks_pushed_updated": pushed_updated,
        "tasks_push_conflicts": push_conflicts,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def sync_user(
    user_id: str,
    db: ReminderDB,
    *,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    """Synchronize a single user's reminders with Google Calendar & Tasks."""
    settings = await db.get_settings(user_id)
    google_sync = settings.get("google_sync", {})

    if not google_sync.get("enabled"):
        return {"status": "disabled", "user_id": user_id}
    if not google_sync.get("refresh_token"):
        return {"status": "unauthorized", "user_id": user_id}
    if not client_id or not client_secret:
        return {"status": "error", "user_id": user_id,
                "error": "GOOGLE_OAUTH_CLIENT_ID/SECRET not configured"}

    # Build + refresh credentials.
    creds = _build_credentials(google_sync, client_id, client_secret)
    try:
        await _refresh_if_needed(creds, db, user_id)
    except Exception as exc:
        logger.exception("Token refresh failed for user %s", user_id)
        await db.update_google_sync(user_id, {
            "status": "error",
            "last_error": f"token_refresh: {exc}",
        })
        return {"status": "error", "user_id": user_id, "error": f"token_refresh: {exc}"}

    # Re-load the updated google_sync (refresh may have written new access_token).
    settings = await db.get_settings(user_id)
    google_sync = settings.get("google_sync", {})

    from googleapiclient.discovery import build

    def _build_services():
        cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
        tsk = build("tasks", "v1", credentials=creds, cache_discovery=False)
        return cal, tsk

    cal_service, tasks_service = await asyncio.to_thread(_build_services)

    summary: dict[str, Any] = {"status": "ok", "user_id": user_id}
    try:
        summary.update(await _calendar_sync(cal_service, db, user_id, google_sync))
    except Exception as exc:
        logger.exception("Calendar sync failed for user %s", user_id)
        summary["calendar_error"] = str(exc)

    try:
        summary.update(await _tasks_sync(tasks_service, db, user_id, google_sync))
    except Exception as exc:
        logger.exception("Tasks sync failed for user %s", user_id)
        summary["tasks_error"] = str(exc)

    had_error = any(k.endswith("_error") for k in summary)
    await db.update_google_sync(user_id, {
        "status": "error" if had_error else "connected",
        "last_sync_at": _now_iso(),
        "last_error": summary.get("calendar_error") or summary.get("tasks_error"),
    })
    if had_error:
        summary["status"] = "partial"
    return summary


async def sync_all_users(
    db: ReminderDB,
    *,
    client_id: str,
    client_secret: str,
) -> list[dict[str, Any]]:
    """Run sync for all users that have Google sync enabled."""
    results = []
    for user_id in db.list_user_ids():
        try:
            result = await sync_user(
                user_id, db, client_id=client_id, client_secret=client_secret,
            )
        except Exception as exc:
            logger.exception("sync_user crashed for %s", user_id)
            result = {"status": "error", "user_id": user_id, "error": str(exc)}
        results.append(result)
    return results


_TICK_SECONDS = 60  # how often the loop wakes up to evaluate per-user schedules


def _due_for_sync(google_sync: dict[str, Any], now: datetime) -> bool:
    """Return True if this user's per-user sync_interval has elapsed."""
    if not google_sync.get("enabled") or not google_sync.get("refresh_token"):
        return False
    last = google_sync.get("last_sync_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    interval_min = max(1, int(google_sync.get("sync_interval") or 15))
    return (now - last_dt).total_seconds() >= interval_min * 60


async def periodic_sync_loop(
    db: ReminderDB,
    *,
    client_id: str,
    client_secret: str,
    interval_min: int = 15,  # default per-user fallback (unused if sync_interval is set)
) -> None:
    """Background loop that runs Google sync on each user's own schedule.

    Wakes every ``_TICK_SECONDS`` and only syncs users whose
    ``settings.google_sync.sync_interval`` minutes have elapsed since their
    ``last_sync_at``.  ``interval_min`` is no longer authoritative — it
    survives only as documentation; per-user ``sync_interval`` (default 15 min,
    settable via the API) controls cadence.
    """
    if not client_id or not client_secret:
        logger.info("Google sync loop disabled — OAuth client not configured.")
        return

    logger.info("Google sync loop started (per-user cadence; tick=%ds)", _TICK_SECONDS)
    await asyncio.sleep(10)  # small startup delay
    while True:
        try:
            now = datetime.now(timezone.utc)
            synced = 0
            for user_id in db.list_user_ids():
                settings = await db.get_settings(user_id)
                gs = settings.get("google_sync") or {}
                if not _due_for_sync(gs, now):
                    continue
                try:
                    await sync_user(
                        user_id, db,
                        client_id=client_id, client_secret=client_secret,
                    )
                    synced += 1
                except Exception:
                    logger.exception("sync_user failed for %s", user_id)
            if synced:
                logger.info("Google sync tick: %d user(s) synced", synced)
        except Exception:
            logger.exception("periodic_sync_loop tick failed")
        await asyncio.sleep(_TICK_SECONDS)
