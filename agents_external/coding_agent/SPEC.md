# Coding Agent

Writes, modifies, and executes code in a sandboxed per-user workspace. Returns results with optional file attachments.

## Calling

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `llmdata.prompt` | str | Yes | Task description |
| `llmdata.context` | str | No | **Important**: Full background context. This agent has no conversation history — provide all relevant details here. |
| `user_id` | str | Yes | Workspace owner |
| `files` | List[ProxyFile] | No | Input files (downloaded to workspace inbox automatically) |
| `timezone` | str | No | IANA timezone (e.g. "Asia/Seoul"). Used by current_time tool. |

## Key capabilities

### File tools
- **read_file** — Read file contents (supports line range with offset/limit)
- **write_file** — Create or overwrite a file
- **edit_file** — Targeted string replacement in a file
- **list_directory** — List directory contents
- **search_file** — Regex search across files

All file tools use the `file_path` argument for paths (relative to workspace).

### Execution
- **execute_command** — Run shell commands in the workspace directory

### Networking
- **web_fetch** — HTTP requests (GET/POST/PUT/DELETE)

### Utility
- **current_time** — Get current date, time, and day of week in the user's timezone

### Output
- **attach_file** — Attach a workspace file to the result. The caller cannot access the workspace — this is the only way to deliver files.

## Result format

Returns `content` (text summary) + optional `files` (attached via attach_file tool).

## Per-user configuration

Each user has configurable: max iterations, max tool calls, allowed/blocked commands, filesystem restrictions, network access. Managed via the agent's web UI.
