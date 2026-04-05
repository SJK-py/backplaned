# Cron Agent

Creates, lists, modifies, and deletes scheduled tasks (cron jobs) that execute autonomously.

## Calling

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `llmdata.prompt` | str | Yes | Natural language instruction |
| `llmdata.context` | str | No | Background context from the caller's conversation |
| `user_id` | str | Yes | Job owner |
| `session_id` | str | Yes | Session ID for report delivery |
| `timezone` | str | No | IANA timezone. Updates user's stored timezone. |

## Key capabilities

- **add_cron_job** — Create a job with cron expression + message prompt. The message should be a self-contained instruction with full context (executed autonomously by another agent).
- **list_cron_jobs** / **get_cron_job** — View jobs.
- **modify_cron_job** — Update expression, message, description, enable/disable, start/end times.
- **remove_cron_job** — Delete a job.

## Cron format

`minute hour day_of_month month day_of_week`
Examples: `0 9 * * *` (daily 9am), `0 */6 * * *` (every 6h), `30 8 * * 1-5` (weekdays 8:30am)

## Example

User: "Every morning at 9am, check whether DeepSeek V4 has been released"

Creates a job with:
- `cron_expression`: `0 9 * * *`
- `message`: "Search for official DeepSeek V4 release news. Report only if a confirmed release is found."

## Autonomous execution

When triggered, the agent executes the job's message with access to all available agents (web search, coding, etc.). Calls report_to_user only if results are noteworthy.
