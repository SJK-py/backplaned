"""
cron_agent/agent.py — Cron job management and autonomous execution agent.

External agent with two modes:
- Interactive: receives LLMData from core agent, manages cron jobs via tools.
- Autonomous: triggered by scheduler, executes job prompt with available agents.
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

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from helper import (
    AgentInfo,
    AgentOutput,
    LLMData,
    ProxyFileManager,
    RouterClient,
    build_openai_tools,
    build_result_request,
    extract_result_text,
    onboard,
)

from config import AgentConfig
from db import CronDB
from tools import CRON_MANAGEMENT_TOOLS, REPORT_TOOL, INBOX_FILE_TOOLS

load_dotenv(Path(__file__).parent / "data" / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cron_agent")

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

agent_config: AgentConfig = None  # type: ignore[assignment]
agent_info: AgentInfo = None  # type: ignore[assignment]
router_client: RouterClient = None  # type: ignore[assignment]
cron_db: CronDB = None  # type: ignore[assignment]
available_destinations: dict[str, Any] = {}

_scheduler_task: Optional[asyncio.Task] = None

# Identifier → Future for pending results (LLM calls + sub-agent spawns)
_pending_results: dict[str, asyncio.Future] = {}

_MAX_ITERATIONS = 15
_MAX_TOOL_CALLS = 30
_AGENT_DOC_PATH = Path(__file__).parent / "AGENT.md"

# ---------------------------------------------------------------------------
# LLM call via llm_agent
# ---------------------------------------------------------------------------


async def _llm_call(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    timeout: float = 120.0,
    model_id: Optional[str] = None,
    tool_choice: Optional[Any] = None,
    user_id: Optional[str] = None,
) -> dict[str, Any]:
    """Call llm_agent via the router and return the normalized response."""
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
            destination_agent_id=agent_config.llm_agent_id,
            payload=payload,
        )
        result_data = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError("LLM call timed out")
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
# Sub-agent spawn with result waiting
# ---------------------------------------------------------------------------


async def _spawn_and_wait(
    dest_agent_id: str,
    payload: dict[str, Any],
    parent_task_id: Optional[str] = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Spawn a sub-agent task and wait for the result."""
    identifier = f"tc_{uuid.uuid4().hex[:12]}"
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = loop.create_future()
    _pending_results[identifier] = fut

    try:
        await router_client.spawn(
            identifier=identifier,
            parent_task_id=parent_task_id,
            destination_agent_id=dest_agent_id,
            payload=payload,
        )
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"Sub-agent '{dest_agent_id}' timed out")
    finally:
        _pending_results.pop(identifier, None)


# ---------------------------------------------------------------------------
# Interactive mode — cron management tool execution
# ---------------------------------------------------------------------------


async def _execute_cron_tool(
    tool_name: str,
    arguments: dict[str, Any],
    user_id: str,
) -> str:
    """Execute a cron management tool and return the result string."""
    if tool_name == "add_cron_job":
        record = await cron_db.add_job(user_id, arguments)
        return json.dumps({"status": "created", "job": record}, indent=2)

    if tool_name == "list_cron_jobs":
        jobs = await cron_db.get_all_jobs(user_id)
        summaries = []
        for jid, j in jobs.items():
            summaries.append({
                "id": jid,
                "description": j.get("description", ""),
                "cron_expression": j.get("cron_expression", ""),
                "enabled": j.get("enabled", True),
                "last_run": j.get("last_run"),
                "run_count": j.get("run_count", 0),
            })
        return json.dumps({"jobs": summaries}, indent=2)

    if tool_name == "get_cron_job":
        job = await cron_db.get_job(user_id, arguments.get("job_id", ""))
        if job is None:
            return json.dumps({"error": "Job not found"})
        return json.dumps({"job": job}, indent=2)

    if tool_name == "modify_cron_job":
        arguments = dict(arguments)
        job_id = arguments.pop("job_id", "")
        job = await cron_db.modify_job(user_id, job_id, arguments)
        if job is None:
            return json.dumps({"error": "Job not found"})
        return json.dumps({"status": "modified", "job": job}, indent=2)

    if tool_name == "remove_cron_job":
        removed = await cron_db.remove_job(user_id, arguments.get("job_id", ""))
        return json.dumps({"status": "removed" if removed else "not_found"})

    return json.dumps({"error": f"Unknown tool: {tool_name}"})


# ---------------------------------------------------------------------------
# Interactive agent loop (cron management)
# ---------------------------------------------------------------------------


async def run_interactive_loop(
    task_id: str,
    parent_task_id: Optional[str],
    llmdata: LLMData,
    user_id: str,
    session_id: str,
    model_id: Optional[str] = None,
    user_tz: str = "UTC",
) -> tuple[int, AgentOutput]:
    """Run the interactive cron management agent loop."""
    from zoneinfo import ZoneInfo as _ZI
    try:
        _tz = _ZI(user_tz)
    except Exception:
        _tz = _ZI("UTC")
        user_tz = "UTC"
    _now = datetime.now(_tz)

    system_prompt = (
        "You are a cron job management agent in a multi-agent system. You create, "
        "list, modify, and delete scheduled tasks (cron jobs) for users.\n\n"
        "## Job creation\n"
        "Convert user descriptions into cron expressions and create jobs with a "
        "self-contained message prompt. The message is executed autonomously by "
        "another agent on each trigger — it must describe what to do and what "
        "constitutes a reportable result, with full context (no conversation history).\n\n"
        "## IMPORTANT: File handling in job messages\n"
        "The autonomous agent executing the job has its own PRIVATE file storage. "
        "When writing job messages that involve file creation:\n"
        "- Instruct the agent to 'create the file and attach it using attach_file'.\n"
        "- NEVER instruct to 'save to a specific path' or 'output/return the file path'.\n"
        "- NEVER reference any file paths like /workspace, /inbox, etc.\n"
        "- The agent uses attach_file to deliver files, and report_to_user to notify.\n\n"
        "## Scheduling\n"
        "Cron format: minute hour day_of_month month day_of_week\n"
        "Examples: '0 9 * * *' = daily 9am, '0 */6 * * *' = every 6 hours, "
        "'30 8 * * 1-5' = weekdays 8:30am\n\n"
        "IMPORTANT: Cron expressions are evaluated in the USER'S TIMEZONE "
        "(shown below). Use the user's local time, NOT UTC.\n"
        "start_at and end_at must be in ISO 8601 UTC format (e.g. 2026-04-04T06:00:00Z).\n\n"
        "Your result goes to another agent — confirm actions clearly."
    )
    if llmdata.agent_instruction:
        system_prompt += f"\n\n[Caller Instruction]\n{llmdata.agent_instruction}"
    if llmdata.context:
        system_prompt += f"\n\n[Caller Context]\n{llmdata.context}"
    # Time info at the end to preserve prompt cache prefix.
    system_prompt += (
        f"\n\nUser: {user_id}, Session: {session_id}\n"
        f"Current time: {_now.strftime('%Y-%m-%d %H:%M %Z')} (timezone: {user_tz})"
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": llmdata.prompt},
    ]
    tools = CRON_MANAGEMENT_TOOLS

    iteration = 0
    total_tool_calls = 0
    prompt_tokens = 0
    completion_tokens = 0

    while iteration < _MAX_ITERATIONS:
        iteration += 1

        try:
            llm_result = await _llm_call(messages, tools, model_id=model_id, user_id=user_id)
        except RuntimeError as exc:
            return 504, AgentOutput(content=f"LLM call failed: {exc}")

        llm_content = llm_result.get("content")
        llm_tool_calls = llm_result.get("tool_calls", [])
        llm_usage = llm_result.get("usage")
        llm_thinking_blocks = llm_result.get("thinking_blocks")
        if llm_usage:
            prompt_tokens += llm_usage.get("prompt_tokens", 0)
            completion_tokens += llm_usage.get("completion_tokens", 0)

        # Build assistant message
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

        if not llm_tool_calls:
            return 200, AgentOutput(content=llm_content or "(No output)")

        for tc in llm_tool_calls:
            total_tool_calls += 1
            if total_tool_calls > _MAX_TOOL_CALLS:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": "Error: tool call limit reached.",
                })
                continue
            result = await _execute_cron_tool(tc["name"], tc.get("arguments", {}), user_id)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

    return 200, AgentOutput(content=llm_content or "Max iterations reached.")


# ---------------------------------------------------------------------------
# Autonomous agent loop (job execution)
# ---------------------------------------------------------------------------


async def run_autonomous(
    user_id: str,
    job: dict[str, Any],
    settings: dict[str, Any],
    model_id: Optional[str] = None,
) -> Optional[str]:
    """
    Run the autonomous agent loop for a triggered cron job.

    Returns the report text if a report was generated, None otherwise.
    """
    global available_destinations

    # Refresh available destinations
    try:
        dest_data = await router_client.get_destinations()
        available_destinations = dest_data.get("available_destinations", {})
    except Exception as exc:
        logger.warning("Failed to refresh destinations: %s", exc)

    job_message = job.get("message", "")
    job_description = job.get("description", "")

    system_prompt = (
        f"You are an autonomous agent in a multi-agent system, executing a scheduled task.\n\n"
        f"## Task\n{job_description}\n\n"
        f"## Execution rules\n"
        f"- Use available tools and agents to investigate the task.\n"
        f"- When calling other agents, provide full context in the prompt — "
        f"they have no prior knowledge of this task.\n\n"
        f"## Reporting results\n"
        f"- report_to_user is the ONLY way to deliver results to the user. "
        f"Without it, the user sees nothing from this job execution.\n"
        f"- Call report_to_user(report='...') with a concise summary.\n"
        f"- To deliver a file: write it with write_file(file='result.txt', content=...), "
        f"then call report_to_user(report='...', file='result.txt').\n"
        f"- If nothing noteworthy was found, end without calling report_to_user.\n\n"
        f"## File handling\n"
        f"- Your inbox is PRIVATE. File tools take just a filename (e.g. 'data.txt').\n"
        f"- NEVER reference your inbox paths in prompts to other agents.\n"
        f"- To send files to another agent, use the files argument (auto-transferred).\n"
        f"- If you need another agent to produce a file, instruct it to "
        f"'create and attach the file'. Do NOT say 'save to path'.\n"
        f"- Result files from sub-agents appear in [Result files:] blocks.\n\n"
    )
    user_tz = settings.get("timezone", "UTC")
    from zoneinfo import ZoneInfo as _ZI
    try:
        _tz = _ZI(user_tz)
    except Exception:
        _tz = _ZI("UTC")
        user_tz = "UTC"
    _now = datetime.now(_tz)
    system_prompt += (
        f"Current time: {_now.strftime('%Y-%m-%d %H:%M %Z')} (timezone: {user_tz})\n"
        f"User: {user_id}"
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": job_message},
    ]

    # Per-user inbox for file handling
    inbox_dir = Path(agent_config.data_dir) / "inboxes" / user_id / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    _endpoint = agent_config.agent_endpoint_url or f"http://localhost:{agent_config.agent_port}"
    pfm = ProxyFileManager(
        inbox_dir=inbox_dir,
        router_url=agent_config.router_url,
        agent_endpoint_url=_endpoint,
    )

    # Build tools: available_destinations as agent tools + report + inbox file tools
    remote_tools = build_openai_tools(available_destinations) if available_destinations else []
    all_tools = remote_tools + [REPORT_TOOL] + INBOX_FILE_TOOLS

    report_text: Optional[str] = None
    report_file_pf: Optional[dict[str, Any]] = None
    iteration = 0
    total_tool_calls = 0
    prompt_tokens = 0
    completion_tokens = 0

    while iteration < _MAX_ITERATIONS:
        iteration += 1

        try:
            llm_result = await _llm_call(
                messages, all_tools,
                timeout=agent_config.tool_timeout,
                model_id=model_id,
                user_id=user_id,
            )
        except RuntimeError as exc:
            logger.error("Autonomous LLM call failed for job %s: %s", job["id"], exc)
            break

        llm_content = llm_result.get("content")
        llm_tool_calls = llm_result.get("tool_calls", [])
        llm_usage = llm_result.get("usage")
        llm_thinking_blocks = llm_result.get("thinking_blocks")
        if llm_usage:
            prompt_tokens += llm_usage.get("prompt_tokens", 0)
            completion_tokens += llm_usage.get("completion_tokens", 0)

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

        if not llm_tool_calls:
            break

        for tc in llm_tool_calls:
            tool_name = tc["name"]
            arguments = tc.get("arguments", {})
            tc_id = tc["id"]
            total_tool_calls += 1

            if total_tool_calls > _MAX_TOOL_CALLS:
                messages.append({
                    "role": "tool", "tool_call_id": tc_id,
                    "content": "Error: tool call limit reached.",
                })
                continue

            # Special: report_to_user
            if tool_name == "report_to_user":
                report_text = arguments.get("report", "")
                report_filename = arguments.get("file")
                if report_filename:
                    resolved = (inbox_dir / report_filename).resolve()
                    try:
                        resolved.relative_to(inbox_dir.resolve())
                        report_file_pf = pfm.resolve(str(resolved))
                    except (ValueError, OSError):
                        report_file_pf = None
                        report_text += f"\n(Warning: file '{report_filename}' not found or access denied)"
                else:
                    report_file_pf = None
                messages.append({
                    "role": "tool", "tool_call_id": tc_id,
                    "content": "Report queued for delivery.",
                })
                continue

            # Inbox file tools
            if tool_name == "list_inbox":
                try:
                    entries: list[str] = []
                    for p in sorted(inbox_dir.rglob("*")):
                        if p.is_file():
                            entries.append(f"  {p.name} ({p.stat().st_size} bytes)")
                    messages.append({"role": "tool", "tool_call_id": tc_id,
                                     "content": "\n".join(entries) if entries else "(inbox is empty)"})
                except Exception as exc:
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": f"Error: {exc}"})
                continue

            if tool_name == "read_file":
                fn = arguments.get("file", "")
                try:
                    resolved = (inbox_dir / fn).resolve()
                    resolved.relative_to(inbox_dir.resolve())
                    text = resolved.read_text(encoding="utf-8", errors="replace")
                    if len(text) > 50000:
                        text = text[:50000] + "\n\n[Truncated]"
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": text})
                except ValueError:
                    messages.append({"role": "tool", "tool_call_id": tc_id,
                                     "content": f"Error: file not found: {fn}"})
                except Exception as exc:
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": f"Error: {exc}"})
                continue

            if tool_name == "write_file":
                fn = arguments.get("file", "")
                try:
                    resolved = (inbox_dir / fn).resolve()
                    resolved.relative_to(inbox_dir.resolve())
                    resolved.parent.mkdir(parents=True, exist_ok=True)
                    resolved.write_text(arguments.get("content", ""), encoding="utf-8")
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": f"File written: {fn}"})
                except ValueError:
                    messages.append({"role": "tool", "tool_call_id": tc_id,
                                     "content": f"Error: invalid filename: {fn}"})
                except Exception as exc:
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": f"Error: {exc}"})
                continue

            if tool_name == "delete_file":
                fn = arguments.get("file", "")
                try:
                    resolved = (inbox_dir / fn).resolve()
                    resolved.relative_to(inbox_dir.resolve())
                    resolved.unlink()
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": f"Deleted: {fn}"})
                except ValueError:
                    messages.append({"role": "tool", "tool_call_id": tc_id,
                                     "content": "Error: access denied — file must be in inbox."})
                except Exception as exc:
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": f"Error: {exc}"})
                continue

            # Handle fetch_agent_documentation locally
            if tool_name == "fetch_agent_documentation":
                from helper import handle_fetch_agent_documentation
                doc_result = await handle_fetch_agent_documentation(
                    arguments.get("agent_id", ""), available_destinations,
                    agent_config.router_url,
                )
                messages.append({"role": "tool", "tool_call_id": tc_id, "content": doc_result})
                continue

            # Sub-agent tools (call_{agent_id})
            if tool_name.startswith("call_"):
                dest = tool_name[5:]
                try:
                    result_data = await _spawn_and_wait(
                        dest, pfm.resolve_in_args(arguments), timeout=agent_config.tool_timeout,
                    )
                    result_text = await extract_result_text(
                        result_data, pfm, path_display_base=inbox_dir,
                    )
                    sc = result_data.get("status_code")
                    if sc and sc >= 400:
                        result_text = f"Agent '{dest}' error ({sc}): {result_text}"
                except RuntimeError as exc:
                    result_text = f"Agent '{dest}' failed: {exc}"

                messages.append({
                    "role": "tool", "tool_call_id": tc_id,
                    "content": result_text,
                })
                continue

            messages.append({
                "role": "tool", "tool_call_id": tc_id,
                "content": f"Unknown tool: {tool_name}",
            })

    # Deliver report if generated
    if report_text:
        await _deliver_report(user_id, settings, report_text, job, file_pf=report_file_pf)

    return report_text


async def _deliver_report(
    user_id: str,
    settings: dict[str, Any],
    report: str,
    job: dict[str, Any],
    file_pf: Optional[dict[str, Any]] = None,
) -> None:
    """Send a report to the user's reporting agent, optionally with a file."""
    reporting_agent = settings.get("reporting_agent_id") or agent_config.core_agent_id
    reporting_session = settings.get("reporting_session_id")
    if not reporting_session:
        logger.warning("No reporting_session_id for user %s — cannot deliver report", user_id)
        return

    file_note = ""
    if file_pf:
        fname = file_pf.get("original_filename") or "attached file"
        file_note = (
            f"\n\nA file ({fname}) is attached with this report. "
            f"Use attach_file to deliver it to the user along with the report message."
        )
    payload: dict[str, Any] = {
        "user_id": user_id,
        "session_id": reporting_session,
        "message": (
            f"Deliver the following scheduled task report to the user.\n\n"
            f"Scheduled task: {job.get('description', 'N/A')}\n\n"
            f"Report:\n{report}"
            f"{file_note}"
        ),
    }
    if file_pf:
        payload["files"] = [file_pf]
    try:
        await router_client.spawn(
            identifier=f"_noreply_cron_{uuid.uuid4().hex[:8]}",
            parent_task_id=None,
            destination_agent_id=reporting_agent,
            payload=payload,
        )
        logger.info("Report delivered for user %s via %s", user_id, reporting_agent)
    except Exception as exc:
        logger.error("Failed to deliver report for user %s: %s", user_id, exc)


# ---------------------------------------------------------------------------
# Task processing
# ---------------------------------------------------------------------------


async def _process_task(
    task_id: str,
    parent_task_id: Optional[str],
    llmdata: LLMData,
    user_id: str,
    session_id: str,
) -> None:
    """Process an interactive task and report result back to router."""
    settings = await cron_db.get_settings(user_id)
    model_id = settings.get("model_id") or agent_config.default_model_id or None
    user_tz = settings.get("timezone", "UTC")

    status_code, output = await run_interactive_loop(
        task_id, parent_task_id, llmdata, user_id, session_id, model_id, user_tz,
    )

    if router_client:
        try:
            result = build_result_request(
                agent_id=agent_config.agent_id,
                task_id=task_id,
                parent_task_id=parent_task_id,
                status_code=status_code,
                output=output,
            )
            await router_client.route(result)
        except Exception as exc:
            logger.error("[%s] Failed to report result: %s", task_id, exc)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global agent_config, agent_info, router_client, cron_db, available_destinations, _scheduler_task

    agent_config = AgentConfig.from_env()

    Path(agent_config.data_dir).mkdir(parents=True, exist_ok=True)
    Path(agent_config.log_dir).mkdir(parents=True, exist_ok=True)

    users_dir = Path(agent_config.data_dir) / "users"
    cron_db = CronDB(users_dir)

    # Credential persistence
    creds_path = Path(agent_config.data_dir) / "credentials.json"
    saved_creds: dict[str, str] = {}
    if creds_path.exists():
        try:
            saved_creds = json.loads(creds_path.read_text())
        except Exception:
            pass

    doc_url = f"file://{_AGENT_DOC_PATH}" if _AGENT_DOC_PATH.exists() else None

    agent_info = AgentInfo(
        agent_id=agent_config.agent_id,
        description=(
            "Cron job agent. Creates, lists, modifies, and deletes scheduled tasks "
            "via natural language. Jobs run autonomously on cron schedules. "
            "Provide instructions in llmdata.prompt with context in llmdata.context."
        ),
        input_schema="llmdata: LLMData, user_id: str, session_id: str, timezone: Optional[str]",
        output_schema="content: str",
        required_input=["llmdata", "user_id", "session_id"],
        documentation_url=doc_url,
    )

    _endpoint_url = agent_config.agent_endpoint_url or f"http://localhost:{agent_config.agent_port}"
    _receive_url = f"{_endpoint_url}/receive"

    if saved_creds.get("auth_token"):
        agent_config.agent_auth_token = saved_creds["auth_token"]
        agent_config.agent_id = saved_creds.get("agent_id", agent_config.agent_id)
        router_client = RouterClient(
            router_url=agent_config.router_url,
            agent_id=agent_config.agent_id,
            auth_token=agent_config.agent_auth_token,
        )
        try:
            await router_client.refresh_from_agent_info(agent_info, endpoint_url=_receive_url)
        except Exception as e:
            logger.warning("Failed to refresh agent info: %s", e)
        try:
            dest_data = await router_client.get_destinations()
            available_destinations = dest_data.get("available_destinations", {})
        except Exception as e:
            logger.warning("Failed to fetch destinations: %s", e)
        logger.info("Using saved credentials for '%s'.", agent_config.agent_id)

    elif agent_config.invitation_token:
        try:
            resp = await onboard(
                router_url=agent_config.router_url,
                invitation_token=agent_config.invitation_token,
                endpoint_url=_receive_url,
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
            creds_path.write_text(json.dumps({
                "agent_id": resp.agent_id,
                "auth_token": resp.auth_token,
            }))
            logger.info("Onboarded as '%s'.", resp.agent_id)
        except Exception as e:
            logger.error("Onboarding failed: %s", e)

    # Start scheduler
    if router_client:
        from scheduler import scheduler_loop
        _scheduler_task = asyncio.create_task(scheduler_loop(
            db=cron_db,
            run_autonomous=run_autonomous,
            check_interval=agent_config.check_interval,
            default_model_id=agent_config.default_model_id,
            log_dir=agent_config.log_dir,
        ))
        logger.info("Scheduler started (interval: %ds)", agent_config.check_interval)

    yield

    if _scheduler_task:
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
    if router_client:
        await router_client.aclose()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Cron Agent", lifespan=lifespan)

_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.post("/refresh-info")
async def refresh_info(request: Request) -> JSONResponse:
    """Re-push AgentInfo and refresh available destinations."""
    if agent_config.agent_auth_token:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not secrets.compare_digest(auth[7:], agent_config.agent_auth_token):
            return JSONResponse(status_code=403, content={"error": "Forbidden"})
    global available_destinations
    if not router_client:
        return JSONResponse({"status": "error", "detail": "Not connected."}, status_code=503)
    agent_info.documentation_url = f"file://{_AGENT_DOC_PATH}" if _AGENT_DOC_PATH.exists() else None
    try:
        _ep = agent_config.agent_endpoint_url or f"http://localhost:{agent_config.agent_port}"
        await router_client.refresh_from_agent_info(agent_info, endpoint_url=f"{_ep}/receive")
        dest_data = await router_client.get_destinations()
        available_destinations = dest_data.get("available_destinations", {})
        return JSONResponse({"status": "refreshed"})
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=502)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "agent_id": agent_config.agent_id if agent_config else "not initialized"})


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


@app.post("/receive")
async def receive_task(request: Request) -> JSONResponse:
    """Receive a task or result delivery from the router."""
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

    # New task — extract LLMData
    raw_llmdata = payload.get("llmdata")
    if not raw_llmdata:
        if router_client:
            result = build_result_request(
                agent_id=agent_config.agent_id,
                task_id=task_id,
                parent_task_id=parent_task_id,
                status_code=400,
                output=AgentOutput(content="Error: llmdata is required."),
            )
            await router_client.route(result)
        return JSONResponse({"status": "error"}, status_code=400)

    llmdata = LLMData.model_validate(raw_llmdata)
    user_id = str(payload.get("user_id") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()

    if not user_id or not session_id:
        if router_client:
            result = build_result_request(
                agent_id=agent_config.agent_id,
                task_id=task_id,
                parent_task_id=parent_task_id,
                status_code=400,
                output=AgentOutput(content="Error: user_id and session_id are required."),
            )
            await router_client.route(result)
        return JSONResponse({"status": "error"}, status_code=400)

    # Capture reporting info
    origin_agent_id = body.get("agent_id")
    await cron_db.ensure_reporting_info(user_id, session_id, origin_agent_id)

    # Update timezone if provided in payload.
    payload_tz = payload.get("timezone")
    if payload_tz:
        await cron_db.update_settings(user_id, {"timezone": payload_tz})

    # Update available_destinations
    global available_destinations
    if "available_destinations" in body:
        available_destinations = body["available_destinations"]

    asyncio.create_task(_process_task(task_id, parent_task_id, llmdata, user_id, session_id))
    return JSONResponse({"status": "accepted"}, status_code=202)


# ---------------------------------------------------------------------------
# Web UI (imported from web_ui.py)
# ---------------------------------------------------------------------------

from web_ui import build_web_router
app.include_router(build_web_router())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    cfg = AgentConfig.from_env()
    uvicorn.run("agent:app", host=cfg.agent_host, port=cfg.agent_port, reload=False)
