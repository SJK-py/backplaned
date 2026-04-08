"""
cron_agent/tools.py — Tool definitions for the cron agent.

Interactive mode: cron job CRUD tools.
Autonomous mode: report_to_user special tool.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Interactive mode tools (cron management)
# ---------------------------------------------------------------------------

CRON_MANAGEMENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "add_cron_job",
            "description": (
                "Create a new cron job. The cron_expression uses standard 5-field "
                "cron format: minute hour day_of_month month day_of_week. "
                "Examples: '0 9 * * *' (daily 9am), '*/30 * * * *' (every 30min), "
                "'0 9 * * 1-5' (weekdays 9am). "
                "The message is the prompt that will be given to the LLM agent "
                "when the cron job triggers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cron_expression": {
                        "type": "string",
                        "description": "5-field cron expression (minute hour dom month dow)",
                    },
                    "message": {
                        "type": "string",
                        "description": "The task prompt to execute on each trigger",
                    },
                    "description": {
                        "type": "string",
                        "description": "Human-readable description of what this job does",
                    },
                    "model_id": {
                        "type": "string",
                        "description": "Optional model config ID to use (defaults to user/global default)",
                    },
                    "start_at": {
                        "type": "string",
                        "description": "ISO datetime in user's local timezone (e.g. 2026-04-09T09:00:00). Job will not run before this time. Recommended for one-time jobs.",
                    },
                    "end_at": {
                        "type": "string",
                        "description": "ISO datetime in user's local timezone (e.g. 2026-04-09T09:02:00). Job auto-disables after this time. Required for one-time jobs — set a few minutes after the intended trigger.",
                    },
                },
                "required": ["cron_expression", "message", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_cron_jobs",
            "description": "List all cron jobs for the current user. Returns a summary of each job.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cron_job",
            "description": "Get full details of a specific cron job by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "The cron job ID"},
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_cron_job",
            "description": (
                "Modify an existing cron job. Only provided fields are updated. "
                "Use enabled=false to pause a job, enabled=true to resume."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "The cron job ID to modify"},
                    "cron_expression": {"type": "string", "description": "New cron expression"},
                    "message": {"type": "string", "description": "New task prompt"},
                    "description": {"type": "string", "description": "New description"},
                    "model_id": {"type": "string", "description": "New model config ID"},
                    "start_at": {"type": "string", "description": "New start time in user's local timezone (e.g. 2026-04-09T09:00:00)"},
                    "end_at": {"type": "string", "description": "New end time in user's local timezone (e.g. 2026-04-09T09:02:00)"},
                    "enabled": {"type": "boolean", "description": "Enable or disable the job"},
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_cron_job",
            "description": "Permanently delete a cron job by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "The cron job ID to delete"},
                },
                "required": ["job_id"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Autonomous mode tools (reporting)
# ---------------------------------------------------------------------------

REPORT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "report_to_user",
        "description": (
            "Send a report to the user, optionally with a file attachment. "
            "Call this ONLY when there is something worth reporting. "
            "If there is nothing noteworthy, do NOT call this tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "report": {
                    "type": "string",
                    "description": "The report message to send to the user. Be concise and informative.",
                },
                "file": {
                    "type": "string",
                    "description": "Optional: filename in inbox to attach to the report.",
                },
            },
            "required": ["report"],
        },
    },
}


# ---------------------------------------------------------------------------
# Inbox file tools (available in autonomous mode)
# ---------------------------------------------------------------------------

INBOX_FILE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the inbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Filename (e.g. 'data.txt')."},
                },
                "required": ["file"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a file to the inbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Filename (e.g. 'output.txt')."},
                    "content": {"type": "string", "description": "Content to write."},
                },
                "required": ["file", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_inbox",
            "description": "List all files in the inbox.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file from the inbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Filename to delete."},
                },
                "required": ["file"],
            },
        },
    },
]
