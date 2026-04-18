"""
reminder_agent/checker.py — Periodic background notification loop (rule-based).

Runs every CHECK_INTERVAL minutes.  For each user:
  1. Checks timezone-aware nighttime window — skips if silent.
  2. Determines check mode: morning (first after nighttime),
     bedtime (last before nighttime), or regular.
  3. Applies window-based rules to select items for notification:
     - Regular: notify if item is due/starts within
       [now + notify_hours, now + notify_hours + check_interval]
       for each configured notify_hours value.
     - Morning: all items due today + all no-due tasks.
     - Bedtime: extended window covering the entire nighttime gap.
  4. Updates last_reminded atomically, builds a notification message,
     and spawns it to core_personal_agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, time as dt_time, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from db import ReminderDB
from tools import _parse_dt, _event_start_dt
from rrule_util import expand_event_occurrences

logger = logging.getLogger("reminder_agent.checker")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_hours_list(value: str) -> list[float]:
    """Parse a comma-separated string of hours into a sorted list of floats."""
    result = []
    for part in value.split(","):
        part = part.strip()
        if part:
            try:
                result.append(float(part))
            except ValueError:
                pass
    return sorted(result)


def _parse_time(s: str) -> Optional[dt_time]:
    try:
        h, m = map(int, s.split(":"))
        return dt_time(h, m)
    except Exception:
        return None


def _is_nighttime(now: datetime, start_str: str, end_str: str) -> bool:
    """Check whether ``now`` falls within the nighttime quiet window."""
    start = _parse_time(start_str)
    end = _parse_time(end_str)
    if start is None or end is None:
        return False

    current = now.time()
    if start <= end:
        return start <= current < end
    else:
        return current >= start or current < end


def _nighttime_end_dt(now: datetime, end_str: str) -> Optional[datetime]:
    """Return the next occurrence of nighttime-end as a tz-aware datetime."""
    end = _parse_time(end_str)
    if end is None:
        return None
    end_dt = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if end_dt <= now:
        end_dt += timedelta(days=1)
    return end_dt


def _is_morning_check(now: datetime, check_interval_min: int,
                      ns: str, ne: str) -> bool:
    """True if the previous check would have been inside the nighttime window."""
    prev = now - timedelta(minutes=check_interval_min)
    return _is_nighttime(prev, ns, ne)


def _is_bedtime_check(now: datetime, check_interval_min: int,
                      ns: str, ne: str) -> bool:
    """True if the next check would fall inside the nighttime window."""
    nxt = now + timedelta(minutes=check_interval_min)
    return _is_nighttime(nxt, ns, ne)


def _in_window(dt: datetime, window_start: datetime, window_end: datetime) -> bool:
    return window_start <= dt < window_end


def _end_of_day(now: datetime) -> datetime:
    """Return 23:59:59 of the same calendar day in the same timezone."""
    return now.replace(hour=23, minute=59, second=59, microsecond=0)


# ---------------------------------------------------------------------------
# Rule-based notification selection
# ---------------------------------------------------------------------------

def _select_regular(
    events: dict[str, Any],
    tasks: dict[str, Any],
    now: datetime,
    check_interval_sec: float,
    event_hours: list[float],
    task_hours: list[float],
    urgent_task_hours: list[float],
    user_tz: ZoneInfo,
) -> list[dict[str, Any]]:
    """Select items for notification using window-based rules."""
    notifications: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # --- Events ---
    max_event_h = max(event_hours) if event_hours else 0
    event_cutoff = now + timedelta(hours=max_event_h) + timedelta(seconds=check_interval_sec)

    for eid, ev in events.items():
        if ev.get("status") == "cancelled":
            continue
        occurrences = expand_event_occurrences(ev, now, event_cutoff, default_tz=user_tz)
        for occ in occurrences:
            occ_start = occ.get("_start_dt") or _event_start_dt(occ, fallback_tz=user_tz)
            if not occ_start:
                continue
            for h in event_hours:
                w_start = now + timedelta(hours=h)
                w_end = w_start + timedelta(seconds=check_interval_sec)
                if _in_window(occ_start, w_start, w_end) and eid not in seen_ids:
                    local_start = occ_start.astimezone(user_tz)
                    hours_until = (occ_start - now).total_seconds() / 3600
                    if hours_until < 0.5:
                        time_note = "starting now"
                    elif hours_until < 1.5:
                        time_note = "in about 1 hour"
                    else:
                        time_note = f"in about {int(hours_until)} hours"
                    summary = occ.get("summary", "Event")
                    location = occ.get("location", "")
                    loc_note = f" at {location}" if location else ""
                    notifications.append({
                        "item_id": eid,
                        "type": "event",
                        "message": f"📅 {summary}{loc_note} — {time_note} ({local_start.strftime('%H:%M')})",
                    })
                    seen_ids.add(eid)
                    break

    # --- Tasks ---
    for tid, tk in tasks.items():
        if tk.get("status") == "completed" or tid in seen_ids:
            continue
        due = _parse_dt(tk.get("due"), default_tz=user_tz)
        if not due:
            continue
        is_urgent = tk.get("urgent", False)
        hours_list = urgent_task_hours if is_urgent else task_hours
        for h in hours_list:
            w_start = now + timedelta(hours=h)
            w_end = w_start + timedelta(seconds=check_interval_sec)
            if _in_window(due, w_start, w_end) and tid not in seen_ids:
                local_due = due.astimezone(user_tz)
                hours_until = (due - now).total_seconds() / 3600
                title = tk.get("title", "Task")
                urgent_mark = "🔴 " if is_urgent else ""
                if hours_until < 0:
                    time_note = "overdue"
                elif hours_until < 0.5:
                    time_note = "due now"
                elif hours_until < 1.5:
                    time_note = "due in about 1 hour"
                else:
                    time_note = f"due in about {int(hours_until)} hours"
                notifications.append({
                    "item_id": tid,
                    "type": "task",
                    "message": f"{urgent_mark}📋 {title} — {time_note} ({local_due.strftime('%H:%M')})",
                })
                seen_ids.add(tid)
                break

    return notifications


def _select_bedtime(
    events: dict[str, Any],
    tasks: dict[str, Any],
    now: datetime,
    nighttime_end_dt: datetime,
    event_hours: list[float],
    task_hours: list[float],
    urgent_task_hours: list[float],
    user_tz: ZoneInfo,
) -> list[dict[str, Any]]:
    """Bedtime check: use (nighttime_end - now) as the effective check_interval."""
    gap_seconds = max((nighttime_end_dt - now).total_seconds(), 0)
    return _select_regular(
        events, tasks, now, gap_seconds,
        event_hours, task_hours, urgent_task_hours, user_tz,
    )


def _select_morning(
    events: dict[str, Any],
    tasks: dict[str, Any],
    now: datetime,
    user_tz: ZoneInfo,
) -> list[dict[str, Any]]:
    """Morning check: all items due today + all no-due tasks."""
    notifications: list[dict[str, Any]] = []
    eod = _end_of_day(now)

    # Events starting today
    for eid, ev in events.items():
        if ev.get("status") == "cancelled":
            continue
        occurrences = expand_event_occurrences(ev, now, eod, default_tz=user_tz)
        for occ in occurrences:
            occ_start = occ.get("_start_dt") or _event_start_dt(occ, fallback_tz=user_tz)
            if not occ_start:
                continue
            local_start = occ_start.astimezone(user_tz)
            summary = occ.get("summary", "Event")
            location = occ.get("location", "")
            loc_note = f" at {location}" if location else ""
            notifications.append({
                "item_id": eid,
                "type": "event",
                "message": f"📅 {summary}{loc_note} — today at {local_start.strftime('%H:%M')}",
            })
            break  # one notification per event

    # Tasks due today + overdue + no-due
    for tid, tk in tasks.items():
        if tk.get("status") == "completed":
            continue
        due = _parse_dt(tk.get("due"), default_tz=user_tz)
        urgent_mark = "🔴 " if tk.get("urgent", False) else ""
        title = tk.get("title", "Task")
        if due:
            if due <= eod:
                local_due = due.astimezone(user_tz)
                if due < now:
                    time_note = "overdue"
                else:
                    time_note = f"due today at {local_due.strftime('%H:%M')}"
                notifications.append({
                    "item_id": tid,
                    "type": "task",
                    "message": f"{urgent_mark}📋 {title} — {time_note}",
                })
        else:
            # No due date — include for morning awareness
            notifications.append({
                "item_id": tid,
                "type": "task",
                "message": f"{urgent_mark}📋 {title} — no due date (reminder)",
            })

    return notifications


# ---------------------------------------------------------------------------
# Notification dispatch
# ---------------------------------------------------------------------------

async def _send_notification(
    router_client: Any,
    core_agent_id: str,
    user_id: str,
    session_id: str,
    message: str,
) -> None:
    """Spawn a task to core_personal_agent to notify the user."""
    payload = {
        "user_id": user_id,
        "session_id": session_id,
        "message": (
            f"You have reminder notifications to deliver to the user. "
            f"Call the channel_agent to send the following message as a direct "
            f"message to the user. Use the user_id and session_id from your "
            f"current session context. Do not modify the notification content.\n\n"
            f"Notification:\n{message}"
        ),
    }
    try:
        await router_client.spawn(
            identifier=f"_noreply_notify_{uuid.uuid4().hex[:8]}",
            parent_task_id=None,
            destination_agent_id=core_agent_id,
            payload=payload,
        )
        logger.info("Notification spawned for user %s via %s", user_id, core_agent_id)
    except Exception as e:
        logger.exception("Failed to spawn notification for user %s: %s", user_id, e)


# ---------------------------------------------------------------------------
# Per-user check
# ---------------------------------------------------------------------------

async def _check_user(
    user_id: str,
    db: ReminderDB,
    router_client: Any,
    core_agent_id: str,
    check_interval_min: int,
    event_notify_hours: list[float],
    task_notify_hours: list[float],
    urgent_task_notify_hours: list[float],
) -> dict[str, Any]:
    """Run the notification check for a single user."""
    settings = await db.get_settings(user_id)
    tz_str = settings.get("timezone", "UTC")
    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        tz = ZoneInfo("UTC")
        tz_str = "UTC"

    now = datetime.now(tz)
    ns = settings.get("nighttime_start", "22:00")
    ne = settings.get("nighttime_end", "07:00")

    if _is_nighttime(now, ns, ne):
        logger.debug("Skipping user %s — nighttime (%s–%s)", user_id, ns, ne)
        return {"user_id": user_id, "action": "skipped", "reason": "nighttime"}

    reporting_sid = settings.get("reporting_session_id")
    if not reporting_sid:
        logger.debug("Skipping user %s — no reporting_session_id", user_id)
        return {"user_id": user_id, "action": "skipped", "reason": "no_reporting_session"}

    user_core_agent = settings.get("reporting_agent_id") or core_agent_id
    events = await db.get_all_events(user_id)
    tasks = await db.get_all_tasks(user_id)

    morning = _is_morning_check(now, check_interval_min, ns, ne)
    bedtime = _is_bedtime_check(now, check_interval_min, ns, ne)

    if morning:
        mode = "morning"
        notifications = _select_morning(events, tasks, now, user_tz=tz)
    elif bedtime:
        ne_dt = _nighttime_end_dt(now, ne)
        if ne_dt is None:
            ne_dt = now + timedelta(hours=9)
        mode = "bedtime"
        notifications = _select_bedtime(
            events, tasks, now, ne_dt,
            event_notify_hours, task_notify_hours, urgent_task_notify_hours,
            user_tz=tz,
        )
    else:
        mode = "regular"
        check_interval_sec = check_interval_min * 60
        notifications = _select_regular(
            events, tasks, now, check_interval_sec,
            event_notify_hours, task_notify_hours, urgent_task_notify_hours,
            user_tz=tz,
        )

    if not notifications:
        logger.debug("No notifications for user %s (mode=%s)", user_id, mode)
        return {"user_id": user_id, "action": "checked", "mode": mode, "notified": 0}

    # Update last_reminded
    notified_ids = list({n["item_id"] for n in notifications})
    await db.update_last_reminded(user_id, notified_ids)

    # Build combined message with mode-appropriate header
    parts = [n["message"] for n in notifications]
    if morning:
        header = f"☀️ Good morning! Here's your schedule for today ({now.strftime('%A, %b %d')}):\n"
        combined = header + "\n".join(parts)
    elif bedtime:
        header = "🌙 Before you go — upcoming reminders for overnight and tomorrow morning:\n"
        combined = header + "\n".join(parts)
    else:
        combined = "\n".join(parts)

    await _send_notification(
        router_client, user_core_agent, user_id, reporting_sid, combined,
    )

    return {
        "user_id": user_id,
        "action": "notified",
        "mode": mode,
        "notified": len(notifications),
        "notified_ids": notified_ids,
    }


# ---------------------------------------------------------------------------
# Checker log persistence
# ---------------------------------------------------------------------------

def _save_checker_log(log_dir: str, log_entry: dict[str, Any]) -> None:
    from pathlib import Path as _Path
    d = _Path(log_dir)
    d.mkdir(parents=True, exist_ok=True)
    cycle_id = log_entry.get("cycle_id", "unknown")
    p = d / f"checker_{cycle_id}.json"
    p.write_text(json.dumps(log_entry, indent=2, default=str, ensure_ascii=False),
                 encoding="utf-8")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def periodic_check_loop(
    db: ReminderDB,
    router_client: Any,
    core_agent_id: str,
    check_interval: int,
    event_notify_hours: list[float],
    task_notify_hours: list[float],
    urgent_task_notify_hours: list[float],
    log_dir: str = "data/logs",
) -> None:
    """
    Background loop that checks all users for pending notifications.

    Runs a check immediately on startup, then sleeps ``check_interval``
    minutes between subsequent cycles.
    """
    logger.info(
        "Periodic checker started: interval=%d min, event_hours=%s, "
        "task_hours=%s, urgent_task_hours=%s",
        check_interval, event_notify_hours, task_notify_hours,
        urgent_task_notify_hours,
    )

    first_run = True
    cycle_count = 0

    while True:
        if first_run:
            await asyncio.sleep(5)
            first_run = False
        else:
            await asyncio.sleep(check_interval * 60)

        cycle_count += 1
        cycle_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{cycle_count}"

        log_entry: dict[str, Any] = {
            "task_id": f"checker_{cycle_id}",
            "cycle_id": cycle_id,
            "type": "checker_cycle",
            "cycle": cycle_count,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "status": "running",
        }

        try:
            user_ids = db.list_user_ids()
            logger.info("Checker cycle %d: checking %d user(s)", cycle_count, len(user_ids))

            user_results: list[dict[str, Any]] = []
            for user_id in user_ids:
                try:
                    result = await _check_user(
                        user_id=user_id,
                        db=db,
                        router_client=router_client,
                        core_agent_id=core_agent_id,
                        check_interval_min=check_interval,
                        event_notify_hours=event_notify_hours,
                        task_notify_hours=task_notify_hours,
                        urgent_task_notify_hours=urgent_task_notify_hours,
                    )
                    user_results.append(result)
                except Exception as exc:
                    logger.exception("Check failed for user %s", user_id)
                    user_results.append({
                        "user_id": user_id,
                        "action": "error",
                        "error": str(exc),
                    })

            total_notified = sum(r.get("notified", 0) for r in user_results)
            log_entry.update(
                status="completed",
                finished_at=datetime.now(timezone.utc).isoformat(),
                users_checked=len(user_ids),
                users=user_results,
                total_notified=total_notified,
            )

        except Exception as exc:
            logger.exception("Periodic check cycle %d failed", cycle_count)
            log_entry.update(
                status="error",
                finished_at=datetime.now(timezone.utc).isoformat(),
                error=str(exc),
            )

        _save_checker_log(log_dir, log_entry)
