"""
reminder_agent/tools.py — Tool definitions and execution engine.

Implements all local tools for managing calendar events and tasks via
the ReminderDB.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from db import ReminderDB
from rrule_util import expand_event_occurrences

logger = logging.getLogger("reminder_agent.tools")

# ---------------------------------------------------------------------------
# OpenAI function-tool definitions
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "add_event",
            "description": (
                "Create a new calendar event/schedule. Returns the created event with its ID. "
                "Use ISO 8601 datetime format (YYYY-MM-DDTHH:MM:SS). For recurring events, "
                "use RRULE format (e.g. 'RRULE:FREQ=WEEKLY;COUNT=4')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Event title/name (required)"},
                    "start_datetime": {"type": "string", "description": "Start date-time in ISO 8601 format (required)"},
                    "end_datetime": {"type": "string", "description": "End date-time in ISO 8601 format (optional, defaults to 1 hour after start)"},
                    "description": {"type": "string", "description": "Event description or notes"},
                    "location": {"type": "string", "description": "Event location"},
                    "recurrence_rule": {"type": "string", "description": "RRULE string for recurring events (e.g. 'RRULE:FREQ=WEEKLY;COUNT=4')"},
                    "timezone": {"type": "string", "description": "Timezone for the event (e.g. 'America/Los_Angeles'). Defaults to user's timezone."},
                    "status": {"type": "string", "enum": ["confirmed", "tentative", "cancelled"], "description": "Event status (default: confirmed)"},
                },
                "required": ["summary", "start_datetime"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_task",
            "description": (
                "Create a new task/to-do item. Returns the created task with its ID. "
                "Tasks may or may not have a due date."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Task title (required)"},
                    "notes": {"type": "string", "description": "Task notes or description"},
                    "due_datetime": {"type": "string", "description": "Due date-time in ISO 8601 format (optional)"},
                    "urgent": {"type": "boolean", "description": "Whether the task is urgent (default: false)"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_items",
            "description": (
                "Query events and/or tasks with optional filters. Returns matching items as JSON."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_type": {"type": "string", "enum": ["events", "tasks", "all"], "description": "Type of items to list (default: all)"},
                    "date_from": {"type": "string", "description": "Filter items from this date (ISO 8601)"},
                    "date_to": {"type": "string", "description": "Filter items up to this date (ISO 8601)"},
                    "status_filter": {"type": "string", "description": "Filter by status (e.g. 'needsAction', 'confirmed', 'completed')"},
                    "urgent_only": {"type": "boolean", "description": "Show only urgent tasks (default: false)"},
                    "include_completed": {"type": "boolean", "description": "Include completed tasks (default: false)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_event",
            "description": "Update an existing calendar event by its ID. Only provided fields are changed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "ID of the event to modify (required)"},
                    "summary": {"type": "string", "description": "New title"},
                    "start_datetime": {"type": "string", "description": "New start date-time"},
                    "end_datetime": {"type": "string", "description": "New end date-time"},
                    "description": {"type": "string", "description": "New description"},
                    "location": {"type": "string", "description": "New location"},
                    "recurrence_rule": {"type": "string", "description": "New RRULE string"},
                    "timezone": {"type": "string", "description": "New timezone"},
                    "status": {"type": "string", "enum": ["confirmed", "tentative", "cancelled"]},
                },
                "required": ["event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_task",
            "description": "Update an existing task by its ID. Only provided fields are changed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "ID of the task to modify (required)"},
                    "title": {"type": "string", "description": "New title"},
                    "notes": {"type": "string", "description": "New notes"},
                    "due_datetime": {"type": "string", "description": "New due date-time"},
                    "urgent": {"type": "boolean", "description": "Urgent flag"},
                    "status": {"type": "string", "enum": ["needsAction", "completed"], "description": "Task status"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_item",
            "description": "Delete an event or task by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string", "description": "ID of the item to remove (required)"},
                    "item_type": {"type": "string", "enum": ["event", "task"], "description": "Type of item (required)"},
                },
                "required": ["item_id", "item_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "Mark a task as completed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "ID of the task to complete (required)"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_agenda",
            "description": (
                "Get a formatted markdown view of upcoming events and pending tasks. "
                "Useful for showing the user their schedule."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["today", "tomorrow", "week", "month"],
                        "description": "Time scope for the agenda (default: today)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_user_settings",
            "description": (
                "Modify the user's reminder settings: nighttime quiet hours, timezone, "
                "or reporting session ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nighttime_start": {"type": "string", "description": "Start of nighttime quiet hours (HH:MM, e.g. '22:00')"},
                    "nighttime_end": {"type": "string", "description": "End of nighttime quiet hours (HH:MM, e.g. '07:00')"},
                    "timezone": {"type": "string", "description": "User timezone (e.g. 'America/Los_Angeles')"},
                    "reporting_session_id": {"type": "string", "description": "Session ID used for proactive notifications"},
                },
                "required": [],
            },
        },
    },
]


def get_tool_definitions() -> list[dict[str, Any]]:
    """Return all available tool definitions."""
    return list(TOOL_DEFINITIONS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dt(
    s: Optional[str],
    default_tz: Optional[ZoneInfo] = None,
) -> Optional[datetime]:
    """Parse an ISO 8601 datetime string, tolerant of common variants.

    If the parsed datetime is naive and *default_tz* is given, localise it
    (i.e. interpret it as local time in that timezone).  Already-aware
    datetimes are returned unchanged.
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None and default_tz is not None:
        dt = dt.replace(tzinfo=default_tz)
    return dt


def _tz_for_event_field(field: dict[str, Any], fallback_tz: Optional[ZoneInfo] = None) -> Optional[ZoneInfo]:
    """Return ZoneInfo for a start/end dict's timeZone, or *fallback_tz*."""
    tz_name = field.get("timeZone")
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    return fallback_tz


def _event_start_dt(
    event: dict[str, Any],
    fallback_tz: Optional[ZoneInfo] = None,
) -> Optional[datetime]:
    """Extract start datetime from a Google Calendar-style event.

    Naive datetimes are localised using the event's ``timeZone`` field
    (falling back to *fallback_tz*).
    """
    start = event.get("start") or {}
    tz = _tz_for_event_field(start, fallback_tz)
    return _parse_dt(start.get("dateTime") or start.get("date"), default_tz=tz)


def _event_end_dt(
    event: dict[str, Any],
    fallback_tz: Optional[ZoneInfo] = None,
) -> Optional[datetime]:
    """Extract end datetime from a Google Calendar-style event."""
    end = event.get("end") or {}
    tz = _tz_for_event_field(end, fallback_tz)
    return _parse_dt(end.get("dateTime") or end.get("date"), default_tz=tz)


def _build_event_data(args: dict[str, Any], user_tz: str) -> dict[str, Any]:
    """Convert tool arguments into a Google Calendar-compatible event dict."""
    tz = args.get("timezone") or user_tz
    start_dt = args["start_datetime"]
    end_dt = args.get("end_datetime")

    # Default end to 1 hour after start.
    if not end_dt:
        parsed_start = _parse_dt(start_dt)
        if parsed_start:
            end_dt = (parsed_start + timedelta(hours=1)).isoformat()

    event: dict[str, Any] = {
        "summary": args["summary"],
        "start": {"dateTime": start_dt, "timeZone": tz},
        "end": {"dateTime": end_dt, "timeZone": tz} if end_dt else {},
    }
    if args.get("description"):
        event["description"] = args["description"]
    if args.get("location"):
        event["location"] = args["location"]
    if args.get("recurrence_rule"):
        event["recurrence"] = [args["recurrence_rule"]]
    if args.get("status"):
        event["status"] = args["status"]
    return event


# ---------------------------------------------------------------------------
# ToolEngine
# ---------------------------------------------------------------------------

class ToolEngine:
    """Executes reminder tools against a per-user ReminderDB."""

    def __init__(self, reminder_db: ReminderDB, user_id: str, user_tz: str = "UTC") -> None:
        self.db = reminder_db
        self.user_id = user_id
        self.user_tz = user_tz

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Execute a tool by name and return the result as a JSON string."""
        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None:
            return json.dumps({"error": f"Unknown tool '{tool_name}'"})
        try:
            result = await handler(arguments)
            return json.dumps(result, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            logger.exception("Tool %s failed: %s", tool_name, e)
            return json.dumps({"error": str(e)})

    # -- Tool implementations -----------------------------------------------

    async def _tool_add_event(self, args: dict[str, Any]) -> dict[str, Any]:
        event_data = _build_event_data(args, self.user_tz)
        record = await self.db.add_event(self.user_id, event_data)
        return {"status": "created", "event": record}

    async def _tool_add_task(self, args: dict[str, Any]) -> dict[str, Any]:
        task_data: dict[str, Any] = {"title": args["title"]}
        if args.get("notes"):
            task_data["notes"] = args["notes"]
        if args.get("due_datetime"):
            task_data["due"] = args["due_datetime"]
        if args.get("urgent") is not None:
            task_data["urgent"] = args["urgent"]
        record = await self.db.add_task(self.user_id, task_data)
        return {"status": "created", "task": record}

    async def _tool_list_items(self, args: dict[str, Any]) -> dict[str, Any]:
        item_type = args.get("item_type", "all")
        try:
            _user_zi = ZoneInfo(self.user_tz)
        except Exception:
            _user_zi = ZoneInfo("UTC")
        date_from = _parse_dt(args.get("date_from"), default_tz=_user_zi)
        date_to = _parse_dt(args.get("date_to"), default_tz=_user_zi)
        status_filter = args.get("status_filter")
        urgent_only = args.get("urgent_only", False)
        include_completed = args.get("include_completed", False)

        result: dict[str, Any] = {}

        if item_type in ("events", "all"):
            all_events = await self.db.get_all_events(self.user_id)

            if date_from and date_to:
                # Expand recurring events within the date window.
                event_list = []
                for ev in all_events.values():
                    if status_filter and ev.get("status") != status_filter:
                        continue
                    occs = expand_event_occurrences(ev, date_from, date_to)
                    for occ in occs:
                        occ.pop("_start_dt", None)
                        event_list.append(occ)
                result["events"] = event_list
            else:
                # No date window — return raw events (no expansion).
                filtered = {}
                for eid, ev in all_events.items():
                    if status_filter and ev.get("status") != status_filter:
                        continue
                    start = _event_start_dt(ev, fallback_tz=_user_zi)
                    if date_from and start and start < date_from:
                        continue
                    if date_to and start and start > date_to:
                        continue
                    filtered[eid] = ev
                result["events"] = filtered

        if item_type in ("tasks", "all"):
            all_tasks = await self.db.get_all_tasks(self.user_id)
            filtered = {}
            for tid, tk in all_tasks.items():
                if not include_completed and tk.get("status") == "completed":
                    continue
                if status_filter and tk.get("status") != status_filter:
                    continue
                if urgent_only and not tk.get("urgent"):
                    continue
                due = _parse_dt(tk.get("due"), default_tz=_user_zi)
                if date_from and due and due < date_from:
                    continue
                if date_to and due and due > date_to:
                    continue
                filtered[tid] = tk
            result["tasks"] = filtered

        return result

    async def _tool_modify_event(self, args: dict[str, Any]) -> dict[str, Any]:
        args = dict(args)
        event_id = args.pop("event_id")
        updates: dict[str, Any] = {}

        if "summary" in args:
            updates["summary"] = args["summary"]
        if "description" in args:
            updates["description"] = args["description"]
        if "location" in args:
            updates["location"] = args["location"]
        if "status" in args:
            updates["status"] = args["status"]
        if "start_datetime" in args:
            tz = args.get("timezone") or self.user_tz
            updates["start"] = {"dateTime": args["start_datetime"], "timeZone": tz}
        if "end_datetime" in args:
            tz = args.get("timezone") or self.user_tz
            updates["end"] = {"dateTime": args["end_datetime"], "timeZone": tz}
        if "recurrence_rule" in args:
            updates["recurrence"] = [args["recurrence_rule"]]

        record = await self.db.modify_event(self.user_id, event_id, updates)
        if record is None:
            return {"error": f"Event '{event_id}' not found"}
        return {"status": "updated", "event": record}

    async def _tool_modify_task(self, args: dict[str, Any]) -> dict[str, Any]:
        args = dict(args)
        task_id = args.pop("task_id")
        updates: dict[str, Any] = {}

        if "title" in args:
            updates["title"] = args["title"]
        if "notes" in args:
            updates["notes"] = args["notes"]
        if "due_datetime" in args:
            updates["due"] = args["due_datetime"]
        if "urgent" in args:
            updates["urgent"] = args["urgent"]
        if "status" in args:
            updates["status"] = args["status"]
            if args["status"] == "completed":
                updates["completed"] = datetime.now(timezone.utc).isoformat()

        record = await self.db.modify_task(self.user_id, task_id, updates)
        if record is None:
            return {"error": f"Task '{task_id}' not found"}
        return {"status": "updated", "task": record}

    async def _tool_remove_item(self, args: dict[str, Any]) -> dict[str, Any]:
        item_id = args["item_id"]
        item_type = args["item_type"]

        if item_type == "event":
            ok = await self.db.remove_event(self.user_id, item_id)
        else:
            ok = await self.db.remove_task(self.user_id, item_id)

        if not ok:
            return {"error": f"{item_type.title()} '{item_id}' not found"}
        return {"status": "removed", "item_id": item_id, "item_type": item_type}

    async def _tool_complete_task(self, args: dict[str, Any]) -> dict[str, Any]:
        record = await self.db.complete_task(self.user_id, args["task_id"])
        if record is None:
            return {"error": f"Task '{args['task_id']}' not found"}
        return {"status": "completed", "task": record}

    async def _tool_get_agenda(self, args: dict[str, Any]) -> dict[str, Any]:
        scope = args.get("scope", "today")

        try:
            tz = ZoneInfo(self.user_tz)
        except Exception:
            tz = ZoneInfo("UTC")

        now = datetime.now(tz)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        if scope == "today":
            date_from = today_start
            date_to = today_start + timedelta(days=1)
            label = now.strftime("%A, %B %d, %Y")
        elif scope == "tomorrow":
            date_from = today_start + timedelta(days=1)
            date_to = today_start + timedelta(days=2)
            label = date_from.strftime("%A, %B %d, %Y")
        elif scope == "week":
            # Start from today, go 7 days out.
            date_from = today_start
            date_to = today_start + timedelta(days=7)
            label = f"{now.strftime('%b %d')} – {(date_to - timedelta(days=1)).strftime('%b %d, %Y')}"
        elif scope == "month":
            date_from = today_start
            date_to = today_start + timedelta(days=30)
            label = f"{now.strftime('%B %Y')}"
        else:
            date_from = today_start
            date_to = today_start + timedelta(days=1)
            label = now.strftime("%A, %B %d, %Y")

        # Gather events in range (expanding recurring events).
        all_events = await self.db.get_all_events(self.user_id)
        events_in_range = []
        for ev in all_events.values():
            if ev.get("status") == "cancelled":
                continue
            occs = expand_event_occurrences(ev, date_from, date_to)
            events_in_range.extend(occs)
        events_in_range.sort(key=lambda e: e.get("_start_dt") or _event_start_dt(e, fallback_tz=tz) or datetime.min.replace(tzinfo=tz))

        # Gather pending tasks.
        all_tasks = await self.db.get_all_tasks(self.user_id)
        pending_tasks = []
        overdue_tasks = []
        for tk in all_tasks.values():
            if tk.get("status") == "completed":
                continue
            due = _parse_dt(tk.get("due"), default_tz=tz)
            if due and due < now:
                overdue_tasks.append(tk)
            else:
                pending_tasks.append(tk)

        # Build markdown.
        lines = [f"## Agenda: {label}", ""]

        if events_in_range:
            lines.append("### Events")
            for ev in events_in_range:
                start = ev.get("_start_dt") or _event_start_dt(ev)
                end = _event_end_dt(ev)
                time_str = start.strftime("%H:%M") if start else "?"
                if end:
                    time_str += f" – {end.strftime('%H:%M')}"
                loc = f" @ {ev['location']}" if ev.get("location") else ""
                recur_tag = " [recurring]" if ev.get("recurrence") else ""
                lines.append(f"- **{time_str}** {ev.get('summary', '(no title)')}{loc}{recur_tag}  (id: {ev['id']})")
                if ev.get("description"):
                    lines.append(f"  _{ev['description']}_")
            lines.append("")

        if overdue_tasks:
            lines.append("### Overdue Tasks")
            for tk in overdue_tasks:
                due = _parse_dt(tk.get("due"))
                due_str = due.strftime("%Y-%m-%d") if due else ""
                urgent = " **[URGENT]**" if tk.get("urgent") else ""
                lines.append(f"- {tk.get('title', '(no title)')} (due: {due_str}){urgent}  (id: {tk['id']})")
            lines.append("")

        if pending_tasks:
            lines.append("### Pending Tasks")
            for tk in pending_tasks:
                due = _parse_dt(tk.get("due"))
                due_str = f" (due: {due.strftime('%Y-%m-%d')})" if due else " (no due date)"
                urgent = " **[URGENT]**" if tk.get("urgent") else ""
                lines.append(f"- {tk.get('title', '(no title)')}{due_str}{urgent}  (id: {tk['id']})")
            lines.append("")

        if not events_in_range and not pending_tasks and not overdue_tasks:
            lines.append("_No events or tasks found for this period._")

        return {"agenda": "\n".join(lines)}

    async def _tool_update_user_settings(self, args: dict[str, Any]) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        for key in ("nighttime_start", "nighttime_end", "timezone", "reporting_session_id"):
            if key in args:
                updates[key] = args[key]
        if not updates:
            return {"error": "No valid settings provided."}
        settings = await self.db.update_settings(self.user_id, updates)
        return {"status": "updated", "settings": settings}
