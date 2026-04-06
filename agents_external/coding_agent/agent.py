"""
coding_agent/agent.py — Main FastAPI application for the Coding Agent.

External agent that receives tasks from the router, runs a multi-turn
LLM tool-calling loop, and reports results back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# Add parent directory for helper.py imports
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from helper import (
    AgentInfo,
    AgentOutput,
    LLMData,
    ProxyFile,
    ProxyFileManager,
    RouterClient,
    build_openai_tools,
    build_result_request,
    build_spawn_request,
    extract_result_text,
    onboard,
)

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

from config import AgentConfig, ConfigManager, UserConfig
from tools import ToolEngine, get_tool_definitions

_AGENT_DOC_PATH = Path(__file__).resolve().parent / "SPEC.md"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("coding_agent")

# ---------------------------------------------------------------------------
# Globals (initialized in lifespan)
# ---------------------------------------------------------------------------

agent_config: AgentConfig = None  # type: ignore[assignment]
agent_info: AgentInfo = None  # type: ignore[assignment]
config_manager: ConfigManager = None  # type: ignore[assignment]
router_client: RouterClient = None  # type: ignore[assignment]
available_destinations: dict[str, Any] = {}

# Identifier → Future for pending LLM call results
_pending_results: dict[str, asyncio.Future] = {}

LLM_AGENT_ID: str = os.environ.get("LLM_AGENT_ID", "llm_agent")
DEFAULT_MODEL_ID: str = os.environ.get("DEFAULT_MODEL_ID", "") or ""


# ---------------------------------------------------------------------------
# Task logging
# ---------------------------------------------------------------------------

def _save_task_log(log_entry: dict[str, Any]) -> None:
    """Persist a task execution log to disk."""
    log_dir = Path(agent_config.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{log_entry['task_id']}.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log_entry, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

_BASE_INSTRUCTION = """\
You are a coding agent in a multi-agent system. You write, modify, and execute \
code to complete tasks delegated by other agents.

## Guidelines
- Read files before modifying. Use edit_file for targeted changes, write_file for new files.
- After changes, verify by re-reading files or running tests/commands.
- All file operations use the file_path argument. NEVER embed file paths in prompt text.
- Your workspace is PRIVATE. No other agent can access it. Inbound files from the \
caller arrive in the workspace inbox automatically.
- When calling other agents, pass files via the files argument (auto-transferred). \
NEVER reference your workspace paths in prompts to other agents.

## Returning results
- The caller CANNOT access your workspace. Always call attach_file for every \
file you created or modified that is relevant to the task.
- When in doubt, attach the file. The caller explicitly asked you to do this work \
so they likely need the output.
- Your result goes to another agent, not directly to a user. Include a clear \
summary, actions taken, and any issues — enough context for the caller to \
understand what happened without seeing your workspace.

## Result structure
### Summary
What was accomplished.
### Actions Taken
- List of actions
### Files Modified
- file_path (new/modified)
### Issues / Notes
- Caveats or follow-up items (if any)\
"""


def build_system_prompt(
    llmdata: LLMData,
    workspace_path: Path,
    user_config: UserConfig,
    inbound_files: list[str],
) -> str:
    """Build the complete system prompt from LLMData and runtime context."""
    parts: list[str] = []

    # Base instruction always included; caller's agent_instruction prepended if provided
    parts.append(_BASE_INSTRUCTION)
    if llmdata.agent_instruction:
        parts.append(f"[Caller Instruction]\n{llmdata.agent_instruction}")

    # Caller context (critical for sub-agent use)
    if llmdata.context:
        parts.append(f"[Caller Context]\n{llmdata.context}")

    # Workspace info (file list excluded — injected into user message for prompt caching)
    parts.append(f"[Workspace Information]\nWorking directory: {workspace_path}")

    # Security boundaries
    boundaries: list[str] = []
    if user_config.limit_to_workspace:
        if user_config.allowed_paths:
            boundaries.append(f"Filesystem access: workspace + {user_config.allowed_paths}")
        else:
            boundaries.append("Filesystem access: workspace only")
    else:
        boundaries.append("Filesystem access: unrestricted")

    boundaries.append(f"Network access: {'enabled' if user_config.allow_network else 'disabled'}")

    if not user_config.allow_all_commands and user_config.blocked_commands:
        boundaries.append(f"Blocked commands: {', '.join(user_config.blocked_commands)}")
    elif user_config.allow_all_commands:
        boundaries.append("Blocked commands: none")

    parts.append("[Security Boundaries]\n" + "\n".join(f"- {b}" for b in boundaries))

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Inbound file handling
# ---------------------------------------------------------------------------

async def download_inbound_files(
    files: list[dict[str, Any]],
    pfm: ProxyFileManager,
    workspace: Path,
) -> list[str]:
    """
    Download inbound ProxyFiles into the workspace inbox via pfm.fetch().

    Returns a list of human-readable file info strings for prompt injection.
    """
    if not files:
        return []

    file_infos: list[str] = []
    for raw_file in files:
        if not isinstance(raw_file, dict) or "protocol" not in raw_file:
            logger.warning(f"Skipping invalid file entry: {raw_file}")
            continue
        try:
            lp = await pfm.fetch(raw_file)
            size = Path(lp).stat().st_size
            rel = str(Path(lp).relative_to(workspace))
            size_str = f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} bytes"
            file_infos.append(f"{rel} ({size_str})")
        except Exception as e:
            logger.warning(f"Failed to fetch inbound file {raw_file.get('path', '?')}: {e}")

    return file_infos


# ---------------------------------------------------------------------------
# Centralized LLM call via llm_agent
# ---------------------------------------------------------------------------


async def _llm_call(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    timeout: float = 120.0,
    model_id: Optional[str] = None,
    tool_choice: Optional[Any] = None,
    user_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    Call llm_agent via the router and return the normalized response.

    Returns dict with 'content' (str|None) and 'tool_calls' (list).
    """
    if not router_client:
        raise RuntimeError("Not connected to router")

    identifier = f"llm_{uuid.uuid4().hex[:12]}"
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = loop.create_future()
    _pending_results[identifier] = fut

    llmcall_payload: dict[str, Any] = {
        "messages": messages,
        "tools": tools,
        "model_id": model_id,
    }
    if tool_choice is not None:
        llmcall_payload["tool_choice"] = tool_choice

    payload: dict[str, Any] = {
        "llmcall": llmcall_payload,
        "user_id": user_id or "not_specified",
    }

    try:
        await router_client.spawn(
            identifier=identifier,
            parent_task_id=None,
            destination_agent_id=LLM_AGENT_ID,
            payload=payload,
        )
        result_data = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError("LLM call timed out waiting for llm_agent response")
    finally:
        _pending_results.pop(identifier, None)

    raw_payload = result_data.get("payload", {})
    status_code = result_data.get("status_code", 200)
    content_str = raw_payload.get("content", "")

    if status_code and status_code >= 400:
        raise RuntimeError(f"llm_agent error ({status_code}): {content_str}")

    try:
        return json.loads(content_str)
    except (json.JSONDecodeError, TypeError):
        return {"content": content_str, "tool_calls": []}


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

async def run_agent_loop(
    task_id: str,
    parent_task_id: Optional[str],
    llmdata: LLMData,
    user_id: str,
    files: list[dict[str, Any]],
    user_timezone: str = "UTC",
) -> tuple[int, AgentOutput]:
    """
    Run the multi-turn agent loop.

    Returns (status_code, AgentOutput).
    """
    user_config = config_manager.get_user_config(user_id)

    # Set up workspace
    workspace = Path(agent_config.workspace_root) / user_id
    workspace.mkdir(parents=True, exist_ok=True)

    # ProxyFileManager for file resolution (fetch + outbound)
    pfm = ProxyFileManager(
        inbox_dir=workspace / "inbox",
        router_url=agent_config.router_url,
        persist=True,
        agent_endpoint_url=agent_config.agent_endpoint_url or f"http://localhost:{agent_config.agent_port}",
    )

    # Download inbound files via pfm.fetch() (auto-registers for outbound reuse)
    inbound_infos = await download_inbound_files(files, pfm, workspace)

    # Build system prompt
    system_prompt = build_system_prompt(llmdata, workspace, user_config, inbound_infos)

    # Build tools
    local_tools = get_tool_definitions(user_config)
    remote_tools = build_openai_tools(available_destinations)
    all_tools = local_tools + remote_tools

    # Initialize tool engine
    engine = ToolEngine(workspace, user_config, agent_config.tool_timeout)

    # Initialize messages
    user_content = llmdata.prompt
    if inbound_infos:
        file_lines = [f"  - {finfo}" for finfo in inbound_infos]
        user_content += "\n\n[Attached files:\n" + "\n".join(file_lines) + "\n]"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    # Loop state
    iteration = 0
    total_tool_calls = 0
    tools_used: list[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    # Files collected via attach_file tool, included in final result
    attached_files: list[dict[str, Any]] = []

    # Task log
    log_entry: dict[str, Any] = {
        "task_id": task_id,
        "user_id": user_id,
        "parent_task_id": parent_task_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
    }

    try:
        while iteration < user_config.max_iterations:
            iteration += 1
            logger.info(f"[{task_id}] Iteration {iteration}/{user_config.max_iterations}")

            # Call LLM via llm_agent
            try:
                llm_result = await _llm_call(
                    messages, all_tools if all_tools else [],
                    timeout=agent_config.llm_timeout,
                    model_id=user_config.model_id or DEFAULT_MODEL_ID or None,
                    user_id=user_id,
                )
            except RuntimeError as exc:
                logger.error(f"[{task_id}] LLM call failed at iteration {iteration}: {exc}")
                log_entry.update(status="timeout", finished_at=datetime.now(timezone.utc).isoformat(),
                                 iterations=iteration, tool_calls=total_tool_calls, errors=[str(exc)])
                _save_task_log(log_entry)
                return 504, AgentOutput(content=f"LLM call failed at iteration {iteration}: {exc}")

            llm_content = llm_result.get("content")
            llm_tool_calls = llm_result.get("tool_calls", [])
            llm_usage = llm_result.get("usage")
            llm_thinking_blocks = llm_result.get("thinking_blocks")
            if llm_usage:
                prompt_tokens += llm_usage.get("prompt_tokens", 0)
                completion_tokens += llm_usage.get("completion_tokens", 0)

            # Build the assistant message dict for history
            assistant_dict: dict[str, Any] = {"role": "assistant"}
            if llm_content:
                assistant_dict["content"] = llm_content
            if llm_tool_calls:
                assistant_dict["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"]),
                        },
                    }
                    for tc in llm_tool_calls
                ]
            if llm_thinking_blocks:
                assistant_dict["thinking_blocks"] = llm_thinking_blocks
            messages.append(assistant_dict)

            # No tool calls — LLM produced final text
            if not llm_tool_calls:
                content = llm_content or "(No output)"
                files_out = [ProxyFile(**pf) for pf in attached_files] if attached_files else None
                log_entry.update(
                    status="completed", status_code=200,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    iterations=iteration, tool_calls=total_tool_calls,
                    llm_tokens={"prompt": prompt_tokens, "completion": completion_tokens},
                    tools_used=list(set(tools_used)),
                    result_summary=content[:200], errors=[],
                )
                _save_task_log(log_entry)
                return 200, AgentOutput(content=content, files=files_out)

            # Process tool calls
            for tc in llm_tool_calls:
                tool_name = tc["name"]
                arguments = tc.get("arguments", {})
                tc_id = tc["id"]

                total_tool_calls += 1
                tools_used.append(tool_name)

                # Check tool call limit
                if total_tool_calls > user_config.max_tool_calls:
                    logger.warning(f"[{task_id}] Max tool calls exceeded ({user_config.max_tool_calls})")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": f"Error: Maximum tool call limit ({user_config.max_tool_calls}) reached. Please wrap up and provide your final result.",
                    })
                    continue

                # Handle current_time
                if tool_name == "current_time":
                    from zoneinfo import ZoneInfo as _ZI
                    try:
                        _tz = _ZI(user_timezone)
                    except Exception:
                        _tz = _ZI("UTC")
                    _now = datetime.now(_tz)
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": (
                        f"{_now.strftime('%Y-%m-%d %H:%M %Z')} ({_now.strftime('%A')})\n"
                        f"Timezone: {user_timezone}"
                    )})
                    continue

                # Handle attach_file (collect files for final result)
                if tool_name == "attach_file":
                    fp = arguments.get("file_path", "")
                    abs_path = (workspace / fp).resolve()
                    try:
                        abs_path.relative_to(workspace.resolve())
                    except ValueError:
                        messages.append({"role": "tool", "tool_call_id": tc_id, "content": f"Error: path escapes workspace: {fp}"})
                        continue
                    if not abs_path.is_file():
                        messages.append({"role": "tool", "tool_call_id": tc_id, "content": f"Error: file not found: {fp}"})
                    else:
                        pf_dict = pfm.resolve(str(abs_path))
                        attached_files.append(pf_dict)
                        messages.append({"role": "tool", "tool_call_id": tc_id, "content": f"File attached: {abs_path.name}"})
                    continue

                # Handle fetch_agent_documentation locally
                if tool_name == "fetch_agent_documentation":
                    from helper import handle_fetch_agent_documentation
                    result = await handle_fetch_agent_documentation(
                        arguments.get("agent_id", ""), available_destinations,
                        agent_config.router_url,
                    )
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": result})
                    continue

                # Handle sub-agent tools (call_{agent_id})
                if tool_name.startswith("call_"):
                    dest_agent_id = tool_name[5:]  # Strip "call_" prefix
                    identifier = f"tc_{uuid.uuid4().hex[:12]}"
                    loop = asyncio.get_running_loop()
                    fut: asyncio.Future[dict[str, Any]] = loop.create_future()
                    _pending_results[identifier] = fut

                    try:
                        spawn_payload = build_spawn_request(
                            agent_id=agent_config.agent_id,
                            identifier=identifier,
                            parent_task_id=task_id,
                            destination_agent_id=dest_agent_id,
                            payload=pfm.resolve_in_args(arguments),
                        )
                        await router_client.route(spawn_payload)
                        # Wait for the result delivery from the router.
                        try:
                            result_data = await asyncio.wait_for(fut, timeout=agent_config.tool_timeout)
                            result_text = await extract_result_text(
                                result_data, pfm, task_id, path_display_base=workspace
                            )
                            sc = result_data.get("status_code")
                            if sc and sc >= 400:
                                result_text = f"Sub-agent '{dest_agent_id}' error ({sc}): {result_text}"
                        except asyncio.TimeoutError:
                            result_text = f"Sub-agent '{dest_agent_id}' timed out after {agent_config.tool_timeout}s."
                    except Exception as e:
                        result_text = f"Error spawning sub-agent '{dest_agent_id}': {e}"
                    finally:
                        _pending_results.pop(identifier, None)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": result_text,
                    })
                    continue

                # Handle local tools
                result = await engine.execute(tool_name, arguments)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result,
                })

        # Max iterations reached
        logger.warning(f"[{task_id}] Max iterations reached ({user_config.max_iterations})")
        # Gather what we have
        last_content = ""
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                last_content = msg["content"]
                break

        content = (
            f"Warning: Maximum iterations ({user_config.max_iterations}) reached.\n\n"
            f"Last output:\n{last_content}" if last_content else
            f"Warning: Maximum iterations ({user_config.max_iterations}) reached with no final output."
        )

        log_entry.update(
            status="max_iterations", status_code=200,
            finished_at=datetime.now(timezone.utc).isoformat(),
            iterations=iteration, tool_calls=total_tool_calls,
            llm_tokens={"prompt": prompt_tokens, "completion": completion_tokens},
            tools_used=list(set(tools_used)),
            result_summary=content[:200], errors=["max_iterations_reached"],
        )
        _save_task_log(log_entry)
        return 200, AgentOutput(content=content)

    except Exception as e:
        logger.exception(f"[{task_id}] Unrecoverable error in agent loop")
        log_entry.update(
            status="error", status_code=500,
            finished_at=datetime.now(timezone.utc).isoformat(),
            iterations=iteration, tool_calls=total_tool_calls,
            errors=[str(e)],
        )
        _save_task_log(log_entry)
        return 500, AgentOutput(content=f"Internal agent error: {e}")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize agent on startup."""
    global agent_config, agent_info, config_manager, router_client, available_destinations

    agent_config = AgentConfig.from_env()

    # Ensure directories exist
    Path(agent_config.workspace_root).mkdir(parents=True, exist_ok=True)
    Path(agent_config.log_dir).mkdir(parents=True, exist_ok=True)

    # Load per-user config
    config_manager = ConfigManager(agent_config.user_config_path)

    # Credential persistence (matches other external agents' pattern)
    data_dir = Path(agent_config.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    creds_path = data_dir / "credentials.json"

    # Try loading saved credentials
    saved_creds: dict[str, str] = {}
    if creds_path.exists():
        try:
            saved_creds = json.loads(creds_path.read_text())
            logger.info(f"Loaded saved credentials for '{saved_creds.get('agent_id')}'")
        except Exception:
            pass

    agent_info = AgentInfo(
        agent_id=agent_config.agent_id,
        description=(
            "Coding agent. Writes, modifies, and executes code in a sandboxed workspace. "
            "Provide task in llmdata.prompt with full context in llmdata.context. "
            "Can return output files via attach_file."
        ),
        input_schema="llmdata: LLMData, user_id: str, files: Optional[List[ProxyFile]], timezone: Optional[str]",
        output_schema="content: str, files: Optional[List[ProxyFile]]",
        required_input=["llmdata", "user_id"],
        documentation_url=f"file://{_AGENT_DOC_PATH}" if _AGENT_DOC_PATH.exists() else None,
    )

    if saved_creds.get("auth_token"):
        agent_config.agent_auth_token = saved_creds["auth_token"]
        agent_config.agent_id = saved_creds.get("agent_id", agent_config.agent_id)
        router_client = RouterClient(
            router_url=agent_config.router_url,
            agent_id=agent_config.agent_id,
            auth_token=agent_config.agent_auth_token,
        )
        try:
            await router_client.refresh_from_agent_info(agent_info)
        except Exception as e:
            logger.warning("Failed to refresh agent info: %s", e)
        try:
            dest_data = await router_client.get_destinations()
            available_destinations = dest_data.get("available_destinations", {})
        except Exception as e:
            logger.warning("Failed to fetch destinations: %s", e)
        logger.info(f"Using saved auth token for agent '{agent_config.agent_id}'.")
    elif agent_config.invitation_token:
        logger.info("Onboarding with router using invitation token...")
        endpoint_url = agent_config.agent_endpoint_url or f"http://localhost:{agent_config.agent_port}"
        try:
            resp = await onboard(
                router_url=agent_config.router_url,
                invitation_token=agent_config.invitation_token,
                endpoint_url=f"{endpoint_url}/receive",
                agent_info=agent_info,
            )
            agent_config.agent_auth_token = resp.auth_token
            agent_config.agent_id = resp.agent_id
            available_destinations = resp.available_destinations
            router_client = RouterClient(
                router_url=agent_config.router_url,
                agent_id=resp.agent_id,
                auth_token=resp.auth_token,
            )
            # Save credentials for next startup
            creds_path.write_text(json.dumps({
                "agent_id": resp.agent_id,
                "auth_token": resp.auth_token,
            }))
            logger.info(f"Onboarded as '{resp.agent_id}'. Credentials saved. Destinations: {list(available_destinations.keys())}")
        except Exception as e:
            logger.error(f"Failed to onboard: {e}. Agent will start without router connection.")
            router_client = None  # type: ignore[assignment]
    else:
        logger.warning("No saved credentials or invitation token. Agent will start without router connection.")
        router_client = None  # type: ignore[assignment]

    logger.info(f"Coding agent started on {agent_config.agent_host}:{agent_config.agent_port}")
    yield

    # Shutdown
    if router_client:
        await router_client.aclose()
app = FastAPI(title="Coding Agent", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/refresh-info")
async def refresh_info(request: Request) -> JSONResponse:
    """Re-push AgentInfo and refresh available destinations."""
    if agent_config.agent_auth_token:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not secrets.compare_digest(auth[7:], agent_config.agent_auth_token):
            return JSONResponse(status_code=403, content={"error": "Forbidden"})
    global available_destinations
    if not router_client:
        return JSONResponse({"status": "error", "detail": "Not connected to router."}, status_code=503)
    agent_info.documentation_url = f"file://{_AGENT_DOC_PATH}" if _AGENT_DOC_PATH.exists() else None
    try:
        await router_client.refresh_from_agent_info(agent_info)
        dest_data = await router_client.get_destinations()
        available_destinations = dest_data.get("available_destinations", {})
        return JSONResponse({"status": "refreshed"})
    except Exception as exc:
        logger.warning("Failed to refresh agent info: %s", exc)
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=502)


@app.post("/receive")
async def receive_task(request: Request) -> JSONResponse:
    """
    Receive a task or result delivery from the router.

    For result deliveries (destination_agent_id is None and identifier is set),
    resolves the pending future so the agent loop can continue.
    For new tasks, processes as before.
    """
    # Verify delivery auth from router
    if agent_config.agent_auth_token:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not secrets.compare_digest(auth[7:], agent_config.agent_auth_token):
            return JSONResponse(status_code=403, content={"error": "Forbidden"})

    body = await request.json()
    task_id = body.get("task_id", "unknown")
    parent_task_id = body.get("parent_task_id")
    payload = body.get("payload", {})

    # Handle result deliveries (destination_agent_id is None = result, not new task)
    identifier = body.get("identifier")
    dest = body.get("destination_agent_id")
    if dest is None and "status_code" in body:
        if identifier and identifier in _pending_results:
            fut = _pending_results.get(identifier)
            if fut and not fut.done():
                fut.set_result(body)
        return JSONResponse({"status": "accepted"}, status_code=202)

    # Update available_destinations if provided
    global available_destinations
    if "available_destinations" in body:
        available_destinations = body["available_destinations"]

    # Extract LLMData
    raw_llmdata = payload.get("llmdata")
    if not raw_llmdata:
        logger.error(f"[{task_id}] No llmdata in payload")
        # Report error back to router
        if router_client:
            output = AgentOutput(content="Error: No llmdata provided in payload. This agent requires llmdata with context and prompt.")
            result = build_result_request(
                agent_id=agent_config.agent_id,
                task_id=task_id,
                parent_task_id=parent_task_id,
                status_code=400,
                output=output,
            )
            await router_client.route(result)
        return JSONResponse({"status": "error", "message": "Missing llmdata"}, status_code=400)

    llmdata = LLMData.model_validate(raw_llmdata)
    user_id = str(payload.get("user_id") or "").strip()
    if not user_id:
        logger.error(f"[{task_id}] No user_id in payload")
        if router_client:
            output = AgentOutput(content="Error: user_id is required.")
            result = build_result_request(
                agent_id=agent_config.agent_id,
                task_id=task_id,
                parent_task_id=parent_task_id,
                status_code=400,
                output=output,
            )
            await router_client.route(result)
        return JSONResponse({"status": "error", "message": "Missing user_id"}, status_code=400)
    # Reject unregistered users
    if user_id not in config_manager.list_users():
        logger.error(f"[{task_id}] Unregistered user_id: {user_id}")
        if router_client:
            output = AgentOutput(content=f"Error: user '{user_id}' is not registered. Pre-registration via admin UI or config file is required.")
            result = build_result_request(
                agent_id=agent_config.agent_id,
                task_id=task_id,
                parent_task_id=parent_task_id,
                status_code=403,
                output=output,
            )
            await router_client.route(result)
        return JSONResponse({"status": "error", "message": f"User '{user_id}' is not registered"}, status_code=403)

    files = payload.get("files", [])
    user_timezone = payload.get("timezone") or "UTC"

    logger.info(f"[{task_id}] Received task for user '{user_id}': {llmdata.prompt[:100]}...")

    # Run agent loop in background so we can respond to router quickly
    asyncio.create_task(_process_task(task_id, parent_task_id, llmdata, user_id, files, user_timezone))

    return JSONResponse({"status": "accepted", "task_id": task_id})


async def _process_task(
    task_id: str,
    parent_task_id: Optional[str],
    llmdata: LLMData,
    user_id: str,
    files: list[dict[str, Any]],
    user_timezone: str = "UTC",
) -> None:
    """Process a task and report result back to router."""
    status_code, output = await run_agent_loop(task_id, parent_task_id, llmdata, user_id, files, user_timezone)

    if router_client:
        try:
            result = build_result_request(
                agent_id=agent_config.agent_id,
                task_id=task_id,
                parent_task_id=parent_task_id,
                status_code=status_code,
                output=output,
            )
            resp = await router_client.route(result)
            logger.info(f"[{task_id}] Result reported to router: {resp.status_code}")
        except Exception as e:
            logger.error(f"[{task_id}] Failed to report result to router: {e}")
    else:
        logger.warning(f"[{task_id}] No router connection, result not reported.")


@app.get("/health")
async def health() -> JSONResponse:
    """Health check endpoint."""
    return JSONResponse({
        "status": "ok",
        "agent_id": agent_config.agent_id if agent_config else "not initialized",
        "router_connected": router_client is not None,
    })


@app.get("/files/serve")
async def serve_file(key: str) -> Response:
    """Serve a local file to the router for proxy ingestion."""
    from fastapi.responses import FileResponse
    local_path = ProxyFileManager.serve_file(key)
    if not local_path or not Path(local_path).exists():
        return JSONResponse({"error": "File not found or key expired"}, status_code=404)
    if Path(local_path).is_symlink():
        return JSONResponse({"error": "Symlinks not allowed"}, status_code=403)
    return FileResponse(local_path)


# ---------------------------------------------------------------------------
# Import and mount Web UI
# ---------------------------------------------------------------------------

from web_ui import create_ui_router

# API routes at /ui/* (login, status, users, logs, onboarding, etc.)
app.include_router(create_ui_router())

# Serve static SPA at root — must be last (catch-all)
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static-ui")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    # Load .env file if present
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    config = AgentConfig.from_env()
    uvicorn.run(
        "agent:app",
        host=config.agent_host,
        port=config.agent_port,
        reload=False,
    )
