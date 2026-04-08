"""
reminder_agent/checker.py — Periodic background notification loop.

Runs every CHECK_INTERVAL minutes.  For each user:
  1. Checks timezone-aware nighttime window — skips if silent.
  2. Gathers upcoming events and due/overdue/urgent tasks.
  3. Filters out recently-reminded items.
  4. Calls LLM with structured output to decide which items to notify.
  5. Wrapper updates last_reminded atomically, then spawns notification
     to core_personal_agent (which forwards via channel_agent DM).
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
from tools import _parse_dt, _event_start_dt, _tz_for_event_field
from rrule_util import expand_event_occurrences

logger = logging.getLogger("reminder_agent.checker")

# ---------------------------------------------------------------------------
# Structured output schema for the LLM
# ---------------------------------------------------------------------------

_NOTIFICATION_TOOL = {
    "type": "function",
    "function": {
        "name": "select_notifications",
        "description": (
            "Given a list of upcoming events and pending tasks, decide which ones "
            "the user should be notified about right now.  Return a list of items "
            "with their IDs and a concise, friendly notification message for each."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "notifications": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "item_id": {"type": "string", "description": "The ID of the item to notify about"},
                            "message": {"type": "string", "description": "Friendly notification message for the user"},
                        },
                        "required": ["item_id", "message"],
                    },
                    "description": "List of items to notify. Empty array if nothing needs notification.",
                },
            },
            "required": ["notifications"],
        },
    },
}


# ---------------------------------------------------------------------------
# Nighttime check
# ---------------------------------------------------------------------------

def _is_nighttime(now: datetime, start_str: str, end_str: str) -> bool:
    """Check whether ``now`` falls within the nighttime quiet window."""
    try:
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
    except (ValueError, AttributeError):
        return False

    current = now.time()
    start = dt_time(sh, sm)
    end = dt_time(eh, em)

    if start <= end:
        # e.g. 08:00 – 18:00
        return start <= current < end
    else:
        # Wraps midnight, e.g. 22:00 – 07:00
        return current >= start or current < end


# ---------------------------------------------------------------------------
# Gather candidates
# ---------------------------------------------------------------------------

def _gather_candidates(
    events: dict[str, Any],
    tasks: dict[str, Any],
    now: datetime,
    lookahead_hours: float,
    user_tz: ZoneInfo,
) -> list[dict[str, Any]]:
    """
    Return list of items that might need notification.

    last_reminded is included in each candidate so the LLM can decide
    whether the item warrants another notification (anti-spam).

    All comparisons use timezone-aware datetimes.  Naive datetimes from
    the DB are localised using the event's ``timeZone`` field (falling
    back to the user's default timezone).  Candidate times sent to the
    LLM are converted to the user's local timezone for clarity.
    """
    cutoff = now + timedelta(hours=lookahead_hours)
    candidates: list[dict[str, Any]] = []

    # Events starting within the lookahead window (with RRULE expansion).
    for eid, ev in events.items():
        if ev.get("status") == "cancelled":
            continue
        occurrences = expand_event_occurrences(ev, now, cutoff, default_tz=user_tz)
        for occ in occurrences:
            occ_start = occ.get("_start_dt") or _event_start_dt(occ, fallback_tz=user_tz) or now
            # Show time in user's local timezone for LLM clarity.
            local_start = occ_start.astimezone(user_tz) if occ_start.tzinfo else occ_start
            candidates.append({
                "item_id": eid,
                "type": "event",
                "summary": occ.get("summary", ""),
                "start": local_start.strftime("%Y-%m-%d %H:%M"),
                "description": occ.get("description", ""),
                "location": occ.get("location", ""),
                "last_reminded": ev.get("last_reminded"),
                "recurring": bool(ev.get("recurrence")),
            })

    # All incomplete tasks: overdue, due within lookahead, or no due date.
    for tid, tk in tasks.items():
        if tk.get("status") == "completed":
            continue

        due = _parse_dt(tk.get("due"), default_tz=user_tz)
        is_urgent = tk.get("urgent", False)

        # Include if: overdue, due within lookahead, urgent, or no due date.
        include = False
        if due:
            if due <= cutoff:
                include = True      # overdue or due soon
        if not include and is_urgent:
            include = True          # urgent (with or without due date)
        if not include and not due:
            include = True          # no due date — let LLM decide

        if include:
            due_display = due.astimezone(user_tz).strftime("%Y-%m-%d %H:%M") if due else None
            candidates.append({
                "item_id": tid,
                "type": "task",
                "title": tk.get("title", ""),
                "due": due_display,
                "urgent": is_urgent,
                "notes": tk.get("notes", ""),
                "last_reminded": tk.get("last_reminded"),
            })

    return candidates


# ---------------------------------------------------------------------------
# LLM call for notification selection
# ---------------------------------------------------------------------------

async def _llm_select_notifications(
    llm_call_fn: Any,
    candidates: list[dict[str, Any]],
    now: datetime,
    user_tz_str: str,
    model_id: str | None = None,
) -> list[dict[str, str]]:
    """Call LLM via llm_agent to get structured notification list."""
    system_prompt = (
        f"You are a reminder notification assistant. "
        f"You are given a list of upcoming events and pending tasks. Decide which "
        f"ones the user should be notified about RIGHT NOW.\n\n"
        f"## Selection guidelines\n"
        f"- Events starting very soon (within 30 min) should definitely be notified.\n"
        f"- Events starting within 1-2 hours should be notified as a heads-up.\n"
        f"- Events further out (1-3 days) may deserve a single advance reminder.\n"
        f"- Overdue tasks should be notified.\n"
        f"- Urgent tasks should be notified.\n"
        f"- Tasks due today or tomorrow deserve a reminder.\n"
        f"- Tasks with no due date are included for awareness — remind occasionally.\n\n"
        f"## Anti-spam rules (IMPORTANT)\n"
        f"Each item has a `last_reminded` field (ISO timestamp or null).\n"
        f"- If `last_reminded` is recent (within the last few hours), do NOT "
        f"notify again unless the event is imminent (starting within 30 min) or "
        f"the task is overdue/urgent.\n"
        f"- For non-urgent tasks with no due date, only remind if `last_reminded` "
        f"is null or more than 24 hours ago.\n"
        f"- When in doubt, consider the item's details (location, description, "
        f"type of event/task) to judge urgency. For example, an event at a "
        f"distant location may need an earlier heads-up, while a quick online "
        f"task may not need repeated reminders.\n"
        f"- If still unclear after considering details, skip the item.\n\n"
        f"Write a short, friendly notification message for each selected item.\n"
        f"Use the select_notifications function to return your selections. "
        f"If nothing needs notification right now, return an empty list.\n\n"
        f"Current time: {now.strftime('%Y-%m-%d %H:%M %Z')} (timezone: {user_tz_str})"
    )

    user_content = (
        f"Here are the candidate items for notification:\n\n"
        f"```json\n{json.dumps(candidates, indent=2, default=str)}\n```"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    tools = [_NOTIFICATION_TOOL]

    try:
        result = await llm_call_fn(
            messages, tools,
            model_id=model_id,
            tool_choice={"type": "function", "function": {"name": "select_notifications"}},
        )
    except Exception as e:
        logger.exception("LLM call failed during notification check: %s", e)
        return []

    # Extract structured output from tool calls in the normalized response.
    tool_calls = result.get("tool_calls", [])
    if not tool_calls:
        return []

    try:
        args = tool_calls[0].get("arguments", {})
        return args.get("notifications", [])
    except (IndexError, AttributeError) as e:
        logger.warning("Failed to parse LLM notification response: %s", e)
        return []


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
            f"You have a reminder notification to deliver to the user. "
            f"Call the channel_agent to send the following direct message "
            f"to the user. Use the user_id and session_id from your current "
            f"session context (do NOT use any session_id mentioned in this text). "
            f"Then report the result.\n\n"
            f"Message to deliver:\n{message}"
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
    llm_call_fn: Any,
    core_agent_id: str,
    lookahead_hours: float,
    model_id: str | None = None,
) -> dict[str, Any]:
    """
    Run the notification check for a single user.

    Returns a result dict suitable for the checker log.
    """
    settings = await db.get_settings(user_id)
    tz_str = settings.get("timezone", "UTC")
    # Per-user model_id override from settings, falling back to global default
    user_model_id = settings.get("model_id") or model_id
    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        tz = ZoneInfo("UTC")
        tz_str = "UTC"

    now = datetime.now(tz)

    # Nighttime check.
    ns = settings.get("nighttime_start", "22:00")
    ne = settings.get("nighttime_end", "07:00")
    if _is_nighttime(now, ns, ne):
        logger.debug("Skipping user %s — nighttime (%s–%s)", user_id, ns, ne)
        return {"user_id": user_id, "action": "skipped", "reason": "nighttime"}

    # Check for reporting session.
    reporting_sid = settings.get("reporting_session_id")
    if not reporting_sid:
        logger.debug("Skipping user %s — no reporting_session_id", user_id)
        return {"user_id": user_id, "action": "skipped", "reason": "no_reporting_session"}

    # Per-user reporting agent, falling back to the global default.
    user_core_agent = settings.get("reporting_agent_id") or core_agent_id

    # Gather candidates.
    events = await db.get_all_events(user_id)
    tasks = await db.get_all_tasks(user_id)
    candidates = _gather_candidates(events, tasks, now, lookahead_hours, user_tz=tz)

    if not candidates:
        logger.debug("No notification candidates for user %s", user_id)
        return {"user_id": user_id, "action": "skipped", "reason": "no_candidates"}

    logger.info("Found %d candidates for user %s — calling LLM", len(candidates), user_id)

    # LLM decides which to notify.
    notifications = await _llm_select_notifications(
        llm_call_fn, candidates, now, tz_str, model_id=user_model_id,
    )

    if not notifications:
        logger.debug("LLM returned no notifications for user %s", user_id)
        return {
            "user_id": user_id,
            "action": "checked",
            "candidates": len(candidates),
            "notified": 0,
        }

    # Update last_reminded atomically, then send notifications.
    notified_ids = [n["item_id"] for n in notifications]
    await db.update_last_reminded(user_id, notified_ids)

    # Combine all messages into one notification.
    combined_parts = []
    for n in notifications:
        combined_parts.append(n["message"])
    combined_message = "\n\n".join(combined_parts)

    await _send_notification(
        router_client, user_core_agent, user_id, reporting_sid, combined_message,
    )

    return {
        "user_id": user_id,
        "action": "notified",
        "candidates": len(candidates),
        "notified": len(notifications),
        "notified_ids": notified_ids,
        "reporting_session_id": reporting_sid,
        "reporting_agent_id": user_core_agent,
    }


# ---------------------------------------------------------------------------
# Checker log persistence
# ---------------------------------------------------------------------------

def _save_checker_log(log_dir: str, log_entry: dict[str, Any]) -> None:
    """Write a checker cycle log to the same logs directory used by the web UI."""
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
    llm_call_fn: Any,
    core_agent_id: str,
    check_interval: int,
    lookahead_hours: float,
    log_dir: str = "data/logs",
    model_id: str | None = None,
) -> None:
    """
    Background loop that checks all users for pending notifications.

    Runs a check immediately on startup, then sleeps ``check_interval``
    minutes between subsequent cycles.  Each cycle writes a log entry to
    ``log_dir`` so it appears in the web UI Logs tab.
    """
    logger.info(
        "Periodic checker started: interval=%d min, lookahead=%.1f h",
        check_interval, lookahead_hours,
    )

    first_run = True
    cycle_count = 0

    while True:
        if first_run:
            # Short delay on first run to let the agent finish startup.
            await asyncio.sleep(5)
            first_run = False
        else:
            await asyncio.sleep(check_interval * 60)

        cycle_count += 1
        cycle_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{cycle_count}"
        cycle_start = datetime.now(timezone.utc).isoformat()

        log_entry: dict[str, Any] = {
            "task_id": f"checker_{cycle_id}",
            "cycle_id": cycle_id,
            "type": "checker_cycle",
            "cycle": cycle_count,
            "started_at": cycle_start,
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
                        llm_call_fn=llm_call_fn,
                        core_agent_id=core_agent_id,
                        lookahead_hours=lookahead_hours,
                        model_id=model_id,
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
