"""
coding_agent/tools.py — Tool definitions and execution engine.

Implements all local tools (filesystem, execution, web, result) with
security enforcement (path validation, command blocking).
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

import httpx

from config import UserConfig


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

def _resolve_and_validate_path(
    path: str,
    workspace: Path,
    user_config: UserConfig,
) -> Path:
    """
    Resolve a path and validate it against workspace/allowed-path restrictions.

    Raises ValueError if the path is outside allowed boundaries.
    """
    resolved = (workspace / path).resolve()

    if not user_config.limit_to_workspace:
        return resolved

    # Check workspace
    if resolved == workspace or _is_subpath(resolved, workspace):
        return resolved

    # Check allowed paths
    for allowed in user_config.allowed_paths:
        allowed_path = Path(allowed).resolve()
        if resolved == allowed_path or _is_subpath(resolved, allowed_path):
            return resolved

    raise ValueError(
        f"Path '{path}' resolves to '{resolved}' which is outside the workspace "
        f"and allowed paths. Workspace: {workspace}"
    )


def _is_subpath(child: Path, parent: Path) -> bool:
    """Check if child is a subdirectory/file under parent."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _check_command(command: str, user_config: UserConfig) -> None:
    """
    Check a command against the blocklist.

    Each blocked entry is matched as a whole-word (word-boundary) pattern so
    that e.g. ``"dd"`` blocks the ``dd`` command but not ``add`` or ``address``.

    Raises ValueError if the command matches a blocked pattern.
    """
    if user_config.allow_all_commands:
        return

    cmd_lower = command.lower().strip()
    for blocked in user_config.blocked_commands:
        pattern = r"(?:^|&&|\|\||;|[|`$\(])\s*" + re.escape(blocked.lower())
        if re.search(pattern, cmd_lower):
            raise ValueError(f"Command blocked: matches blocked pattern '{blocked}'")


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents. Returns the full file or a specific line range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "File path relative to workspace."},
                    "offset": {"type": "integer", "description": "Starting line number (1-based). Optional."},
                    "limit": {"type": "integer", "description": "Max number of lines to read. Optional."},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "File path relative to workspace."},
                    "content": {"type": "string", "description": "Content to write."},
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Apply a targeted string replacement in a file. The old_string must match exactly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "File path relative to workspace."},
                    "old_string": {"type": "string", "description": "Exact string to find and replace."},
                    "new_string": {"type": "string", "description": "Replacement string."},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences (default false)."},
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and subdirectories at the given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Directory path relative to workspace. Use '.' for workspace root."},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_file",
            "description": "Search file contents by regex pattern. Returns matching lines with file paths and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for."},
                    "file_path": {"type": "string", "description": "Directory or file to search in, relative to workspace. Defaults to '.'."},
                    "glob": {"type": "string", "description": "Glob pattern to filter files (e.g. '*.py'). Optional."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "Run a shell command in the workspace directory. Returns stdout, stderr, and exit code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute."},
                    "timeout": {"type": "integer", "description": "Timeout in seconds. Optional, uses default if not specified."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch content from a URL. Returns the response body as text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch."},
                    "method": {"type": "string", "description": "HTTP method (default GET).", "enum": ["GET", "POST", "PUT", "DELETE"]},
                    "headers": {"type": "object", "description": "Optional HTTP headers as key-value pairs."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "attach_file",
            "description": "Attach a file to your result so the caller can receive it. The caller cannot access your workspace — this is the ONLY way to deliver files. Call this for each file you want to send, then write your text response as usual.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "File path relative to workspace.",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "current_time",
            "description": "Get the current date, time, and day of week in the user's timezone.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def get_tool_definitions(user_config: UserConfig) -> list[dict[str, Any]]:
    """
    Return tool definitions filtered by user config.

    Removes web_fetch if network is disabled.
    """
    tools = list(TOOL_DEFINITIONS)
    if not user_config.allow_network:
        tools = [t for t in tools if t["function"]["name"] != "web_fetch"]
    return tools


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

class ToolEngine:
    """
    Executes tools with security enforcement.

    All filesystem tools resolve paths relative to the workspace directory.
    """

    def __init__(
        self,
        workspace: Path,
        user_config: UserConfig,
        tool_timeout: int = 60,
    ) -> None:
        self.workspace = workspace.resolve()
        self.user_config = user_config
        self.tool_timeout = tool_timeout

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """
        Execute a tool by name and return the result as a string.

        Raises ValueError for security violations.
        Returns error strings (not exceptions) for tool-level failures,
        so the LLM can see and react to them.
        """
        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None:
            return f"Error: Unknown tool '{tool_name}'."
        try:
            return await handler(arguments)
        except ValueError as e:
            return f"Security error: {e}"
        except Exception as e:
            return f"Error executing {tool_name}: {type(e).__name__}: {e}"

    # -- Filesystem tools --------------------------------------------------

    async def _tool_read_file(self, args: dict[str, Any]) -> str:
        path = _resolve_and_validate_path(args["file_path"], self.workspace, self.user_config)
        if not path.is_file():
            return f"Error: '{args['file_path']}' is not a file or does not exist."

        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        offset = args.get("offset", 1)
        if offset < 1:
            offset = 1
        limit = args.get("limit")

        start = offset - 1  # Convert to 0-based
        if limit:
            end = start + limit
        else:
            end = len(lines)

        selected = lines[start:end]
        numbered = [f"{i + offset}\t{line}" for i, line in enumerate(selected)]

        total = len(lines)
        header = f"[{args['file_path']}] Lines {offset}-{min(offset + len(selected) - 1, total)} of {total}"
        return header + "\n" + "\n".join(numbered)

    async def _tool_write_file(self, args: dict[str, Any]) -> str:
        path = _resolve_and_validate_path(args["file_path"], self.workspace, self.user_config)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        return f"File written: {args['file_path']} ({len(args['content'])} bytes)"

    async def _tool_edit_file(self, args: dict[str, Any]) -> str:
        path = _resolve_and_validate_path(args["file_path"], self.workspace, self.user_config)
        if not path.is_file():
            return f"Error: '{args['file_path']}' does not exist."

        content = path.read_text(encoding="utf-8")
        old_string = args["old_string"]
        new_string = args["new_string"]
        replace_all = args.get("replace_all", False)

        count = content.count(old_string)
        if count == 0:
            return f"Error: old_string not found in '{args['file_path']}'. No changes made."
        if count > 1 and not replace_all:
            return (
                f"Error: old_string found {count} times in '{args['file_path']}'. "
                f"Set replace_all=true to replace all, or provide a more specific string."
            )

        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        path.write_text(new_content, encoding="utf-8")
        replacements = count if replace_all else 1
        return f"File edited: {args['file_path']} ({replacements} replacement(s) made)"

    async def _tool_list_directory(self, args: dict[str, Any]) -> str:
        path = _resolve_and_validate_path(args["file_path"], self.workspace, self.user_config)
        if not path.is_dir():
            return f"Error: '{args['file_path']}' is not a directory."

        entries: list[str] = []
        try:
            for item in sorted(path.iterdir()):
                try:
                    rel = item.relative_to(self.workspace)
                except ValueError:
                    rel = item
                suffix = "/" if item.is_dir() else f" ({item.stat().st_size} bytes)"
                entries.append(f"  {rel}{suffix}")
        except PermissionError:
            return f"Error: Permission denied reading '{args['file_path']}'."

        if not entries:
            return f"Directory '{args['file_path']}' is empty."
        return f"[{args['file_path']}] {len(entries)} entries:\n" + "\n".join(entries)

    async def _tool_search_file(self, args: dict[str, Any]) -> str:
        pattern_str = args["pattern"]
        search_path = args.get("file_path", ".")
        glob_pattern = args.get("glob")

        base = _resolve_and_validate_path(search_path, self.workspace, self.user_config)

        try:
            regex = re.compile(pattern_str)
        except re.error as e:
            return f"Error: Invalid regex pattern: {e}"

        results: list[str] = []
        max_results = 100

        if base.is_file():
            files_to_search = [base]
        elif glob_pattern:
            files_to_search = sorted(base.rglob(glob_pattern))
        else:
            files_to_search = sorted(base.rglob("*"))

        for file_path in files_to_search:
            if not file_path.is_file():
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except (PermissionError, OSError):
                continue
            for line_num, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    try:
                        rel = file_path.relative_to(self.workspace)
                    except ValueError:
                        rel = file_path
                    results.append(f"  {rel}:{line_num}: {line.rstrip()}")
                    if len(results) >= max_results:
                        results.append(f"  ... (truncated at {max_results} results)")
                        return f"Search results for '{pattern_str}':\n" + "\n".join(results)

        if not results:
            return f"No matches found for pattern '{pattern_str}' in '{search_path}'."
        return f"Search results for '{pattern_str}' ({len(results)} matches):\n" + "\n".join(results)

    # -- Execution tools ---------------------------------------------------

    async def _tool_execute_command(self, args: dict[str, Any]) -> str:
        command = args["command"]
        timeout = args.get("timeout", self.tool_timeout)

        _check_command(command, self.user_config)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"Tool execution timed out after {timeout}s. The command was terminated."
        except Exception as e:
            return f"Error running command: {e}"

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        # Truncate large outputs
        max_output = 50_000
        if len(stdout_text) > max_output:
            stdout_text = stdout_text[:max_output] + "\n... (stdout truncated)"
        if len(stderr_text) > max_output:
            stderr_text = stderr_text[:max_output] + "\n... (stderr truncated)"

        parts = [f"Exit code: {proc.returncode}"]
        if stdout_text:
            parts.append(f"stdout:\n{stdout_text}")
        if stderr_text:
            parts.append(f"stderr:\n{stderr_text}")
        return "\n".join(parts)

    # -- Web tools ---------------------------------------------------------

    async def _tool_web_fetch(self, args: dict[str, Any]) -> str:
        if not self.user_config.allow_network:
            return "Error: Network access is disabled for this user."

        url = args["url"]
        method = args.get("method", "GET").upper()
        headers = args.get("headers", {})

        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.request(method, url, headers=headers)
                text = resp.text
                # Truncate large responses
                max_len = 100_000
                if len(text) > max_len:
                    text = text[:max_len] + "\n... (response truncated)"
                return f"HTTP {resp.status_code}\n{text}"
        except httpx.TimeoutException:
            return f"Error: Request to {url} timed out."
        except Exception as e:
            return f"Error fetching {url}: {e}"
