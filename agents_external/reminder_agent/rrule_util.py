"""
reminder_agent/rrule_util.py — RRULE expansion utility.

Expands Google Calendar-compatible recurrence rules into concrete
occurrence datetimes using ``dateutil.rrule``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr


def expand_event_occurrences(
    event: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
    max_occurrences: int = 200,
    default_tz: Optional[ZoneInfo] = None,
) -> list[dict[str, Any]]:
    """
    Expand a recurring event into individual occurrence dicts within a time window.

    Each returned dict has the same fields as the original event but with
    ``start`` / ``end`` adjusted to that occurrence, plus an ``occurrence_index``
    field (0-based).  Non-recurring events are returned as a single-element list
    if they fall within the window.

    Datetimes are localised using the event's ``timeZone`` field (falling back
    to *default_tz*) so that all comparisons are timezone-aware.

    Args:
        event:      A Google Calendar-compatible event dict.
        window_start: Earliest occurrence start (inclusive, tz-aware).
        window_end:   Latest occurrence start (exclusive, tz-aware).
        max_occurrences: Safety cap.
        default_tz: Fallback timezone for naive datetimes.

    Returns:
        List of occurrence dicts, sorted by start time.
    """
    start_obj = event.get("start") or {}
    end_obj = event.get("end") or {}
    start_str = start_obj.get("dateTime") or start_obj.get("date")
    end_str = end_obj.get("dateTime") or end_obj.get("date")

    if not start_str:
        return []

    # Determine the event's timezone.
    tz_name = start_obj.get("timeZone") or end_obj.get("timeZone") or ""
    event_tz: Optional[ZoneInfo] = None
    if tz_name:
        try:
            event_tz = ZoneInfo(tz_name)
        except Exception:
            pass
    if event_tz is None:
        event_tz = default_tz

    try:
        base_start = datetime.fromisoformat(start_str)
    except (ValueError, TypeError):
        return []

    # Localise if naive.
    if base_start.tzinfo is None and event_tz is not None:
        base_start = base_start.replace(tzinfo=event_tz)

    # Compute event duration for shifting end times.
    duration = timedelta(0)
    if end_str:
        try:
            base_end = datetime.fromisoformat(end_str)
            if base_end.tzinfo is None and event_tz is not None:
                base_end = base_end.replace(tzinfo=event_tz)
            duration = base_end - base_start
        except (ValueError, TypeError):
            pass

    # Ensure window bounds are tz-aware for comparison.
    if window_start.tzinfo is None and event_tz is not None:
        window_start = window_start.replace(tzinfo=event_tz)
    if window_end.tzinfo is None and event_tz is not None:
        window_end = window_end.replace(tzinfo=event_tz)

    recurrence = event.get("recurrence") or []

    if not recurrence:
        # Non-recurring: return single occurrence if within window.
        if window_start <= base_start < window_end:
            return [_make_occurrence(event, base_start, duration, 0, tz_name)]
        return []

    # rrule works with naive datetimes — strip tz, expand, then reattach.
    base_naive = base_start.replace(tzinfo=None)
    win_start_naive = window_start.astimezone(base_start.tzinfo).replace(tzinfo=None) if window_start.tzinfo and base_start.tzinfo else window_start.replace(tzinfo=None)
    win_end_naive = window_end.astimezone(base_start.tzinfo).replace(tzinfo=None) if window_end.tzinfo and base_start.tzinfo else window_end.replace(tzinfo=None)

    occurrences: list[dict[str, Any]] = []
    for rule_str in recurrence:
        try:
            rule = rrulestr(rule_str, dtstart=base_naive)
        except (ValueError, TypeError):
            continue

        idx = 0
        for dt in rule:
            if len(occurrences) >= max_occurrences:
                break
            dt_naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
            if dt_naive >= win_end_naive:
                break
            if dt_naive >= win_start_naive:
                # Reattach original timezone.
                dt_aware = dt_naive.replace(tzinfo=base_start.tzinfo) if base_start.tzinfo else dt_naive
                occurrences.append(_make_occurrence(event, dt_aware, duration, idx, tz_name))
            idx += 1

    occurrences.sort(key=lambda o: o["_start_dt"])
    return occurrences


def _make_occurrence(
    event: dict[str, Any],
    start_dt: datetime,
    duration: timedelta,
    index: int,
    tz_name: str,
) -> dict[str, Any]:
    """Build an occurrence dict from the base event and a specific start time."""
    end_dt = start_dt + duration
    occ: dict[str, Any] = {
        "id": event.get("id", ""),
        "summary": event.get("summary", ""),
        "description": event.get("description", ""),
        "location": event.get("location", ""),
        "start": {"dateTime": start_dt.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": tz_name},
        "recurrence": event.get("recurrence", []),
        "status": event.get("status", "confirmed"),
        "last_reminded": event.get("last_reminded"),
        "occurrence_index": index,
        "_start_dt": start_dt,  # internal, for sorting / comparison
    }
    return occ


def next_occurrence_after(
    event: dict[str, Any],
    after: datetime,
    max_search: int = 500,
) -> Optional[datetime]:
    """
    Return the next occurrence start datetime at or after ``after``.

    Returns None if no future occurrence exists.
    """
    start_obj = event.get("start") or {}
    start_str = start_obj.get("dateTime") or start_obj.get("date")
    if not start_str:
        return None

    try:
        base_start = datetime.fromisoformat(start_str)
    except (ValueError, TypeError):
        return None

    base_naive = base_start.replace(tzinfo=None) if base_start.tzinfo else base_start
    after_naive = after.replace(tzinfo=None) if after.tzinfo else after

    recurrence = event.get("recurrence") or []

    if not recurrence:
        return base_naive if base_naive >= after_naive else None

    for rule_str in recurrence:
        try:
            rule = rrulestr(rule_str, dtstart=base_naive)
        except (ValueError, TypeError):
            continue
        count = 0
        for dt in rule:
            dt_naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
            if dt_naive >= after_naive:
                return dt_naive
            count += 1
            if count >= max_search:
                break

    return None
