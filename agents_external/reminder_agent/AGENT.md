# Reminder Agent

Manages per-user calendar events, tasks, and reminders. Sends proactive notifications for due items.

## Calling

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `llmdata.prompt` | str | Yes | Natural language instruction |
| `llmdata.context` | str | No | Background context from the caller's conversation |
| `user_id` | str | Yes | User identifier |
| `session_id` | str | Yes | Session ID for notification delivery |
| `timezone` | str | No | IANA timezone (e.g. "Asia/Seoul"). Updates user's stored timezone. |

## Key capabilities

- **Events** — Create, list, modify, delete calendar events. Supports recurrence (RRULE format).
- **Tasks** — Create, list, complete, delete to-do items. Supports urgent flag and due dates.
- **Agenda** — Show upcoming events and pending tasks for a date range.
- **Settings** — Update timezone, nighttime quiet hours, reporting session.

## Example prompts

- "Add a meeting tomorrow at 3pm called 'Team Standup'"
- "Show my agenda for this week"
- "Add an urgent task to review the PR"
- "Set my timezone to America/New_York"

## Notifications

Periodically checks for upcoming events and overdue tasks. Sends notifications via core_personal_agent during non-quiet hours.
