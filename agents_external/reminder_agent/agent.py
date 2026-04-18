"""
reminder_agent/agent.py — Main FastAPI application for the Reminder Agent.

External agent that manages per-user calendar events and tasks via an LLM
tool-calling loop.  Also runs a periodic background checker that notifies
users of upcoming events and due tasks through core_personal_agent.

Input payload schema:
    llmdata:    LLMData  — natural language instruction
    user_id:    str      — identifies the user
    session_id: str      — identifies the chat session
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
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Add parent directory for helper.py imports
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from helper import (
    AgentInfo,
    AgentOutput,
    LLMData,
    RouterClient,
    build_openai_tools,
    build_result_request,
    onboard,
)

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / "data" / ".env")

from config import AgentConfig
from db import ReminderDB
from tools import ToolEngine, get_tool_definitions
from checker import periodic_check_loop

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("reminder_agent")

# ---------------------------------------------------------------------------
# Globals (initialized in lifespan)
# ---------------------------------------------------------------------------

agent_config: AgentConfig = None  # type: ignore[assignment]
agent_info: AgentInfo = None  # type: ignore[assignment]
router_client: RouterClient = None  # type: ignore[assignment]
reminder_db: ReminderDB = None  # type: ignore[assignment]
available_destinations: dict[str, Any] = {}

_checker_task: Optional[asyncio.Task] = None

# Identifier → Future for pending LLM call results
_pending_llm: dict[str, asyncio.Future] = {}

# Identifier → Future for pending sub-agent call results
_pending_sub: dict[str, asyncio.Future] = {}

LLM_AGENT_ID: str = os.environ.get("LLM_AGENT_ID", "llm_agent")
def _get_default_model_id() -> str:
    from config import _load_config
    return _load_config().get("DEFAULT_MODEL_ID") or os.environ.get("DEFAULT_MODEL_ID", "") or ""

# Max iterations for the LLM tool-calling loop
_MAX_ITERATIONS = 20
_MAX_TOOL_CALLS = 50

# ---------------------------------------------------------------------------
# Task logging
# ---------------------------------------------------------------------------


def _save_task_log(log_entry: dict[str, Any]) -> None:
    log_dir = Path(agent_config.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{log_entry['task_id']}.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log_entry, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

_BASE_INSTRUCTION = """\
You are a reminder and schedule management agent in a multi-agent system. \
You manage calendar events, tasks, and reminders for users.

## Guidelines
- Time-specific requests → create an event. To-do items → create a task.
- Recurring events: use RRULE format (e.g. 'RRULE:FREQ=WEEKLY;COUNT=4').
- Use get_agenda or list_items to show schedules.
- ISO 8601 for all dates/times. Compute relative dates from current time below.
- Your result goes to another agent — confirm actions clearly with enough \
detail for the caller to relay to the user.\
"""


def _build_system_prompt(llmdata: LLMData, user_tz: str) -> str:
    """Build the system prompt with current time and user timezone."""
    parts: list[str] = []

    parts.append(_BASE_INSTRUCTION)
    if llmdata.agent_instruction:
        parts.append(f"[Caller Instruction]\n{llmdata.agent_instruction}")
    if llmdata.context:
        parts.append(f"[Caller Context]\n{llmdata.context}")

    try:
        tz = ZoneInfo(user_tz)
    except Exception:
        tz = ZoneInfo("UTC")
        user_tz = "UTC"

    now = datetime.now(tz)
    parts.append(
        f"[Current Time]\n"
        f"{now.strftime('%Y-%m-%d %H:%M %Z')} ({now.strftime('%A')})\n"
        f"Timezone: {user_tz}"
    )

    return "\n\n".join(parts)


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
    """Call llm_agent via the router and return the normalized response."""
    if not router_client:
        raise RuntimeError("Not connected to router")

    identifier = f"llm_{uuid.uuid4().hex[:12]}"
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = loop.create_future()
    _pending_llm[identifier] = fut

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
        _pending_llm.pop(identifier, None)

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
    session_id: str,
    task_available_destinations: dict[str, Any],
) -> tuple[int, AgentOutput]:
    """Run the multi-turn LLM tool-calling loop. Returns (status_code, AgentOutput)."""

    # Load user settings for timezone and model.
    settings = await reminder_db.get_settings(user_id)
    user_tz = settings.get("timezone", "UTC")
    user_model_id = settings.get("model_id") or _get_default_model_id() or None

    # Build system prompt.
    system_prompt = _build_system_prompt(llmdata, user_tz)

    # Build tools.
    local_tools = get_tool_definitions()
    remote_tools = build_openai_tools(task_available_destinations)
    all_tools = local_tools + remote_tools

    # Initialize tool engine.
    engine = ToolEngine(reminder_db, user_id, user_tz)

    # Initialize messages.
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": llmdata.prompt},
    ]

    # Loop state.
    iteration = 0
    total_tool_calls = 0
    tools_used: list[str] = []
    prompt_tokens = 0
    completion_tokens = 0

    log_entry: dict[str, Any] = {
        "task_id": task_id,
        "user_id": user_id,
        "session_id": session_id,
        "parent_task_id": parent_task_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
    }

    try:
        while iteration < _MAX_ITERATIONS:
            iteration += 1
            logger.info("[%s] Iteration %d/%d", task_id, iteration, _MAX_ITERATIONS)

            # Call LLM via llm_agent.
            try:
                llm_result = await _llm_call(
                    messages, all_tools if all_tools else [],
                    timeout=agent_config.llm_timeout,
                    model_id=user_model_id,
                    user_id=user_id,
                )
            except RuntimeError as exc:
                logger.error("[%s] LLM call failed at iteration %d: %s", task_id, iteration, exc)
                log_entry.update(status="timeout", finished_at=datetime.now(timezone.utc).isoformat(),
                                 iterations=iteration, tool_calls=total_tool_calls, errors=[str(exc)])
                _save_task_log(log_entry)
                return 504, AgentOutput(content=f"LLM call failed: {exc}")

            llm_content = llm_result.get("content")
            llm_tool_calls = llm_result.get("tool_calls", [])
            llm_usage = llm_result.get("usage")
            llm_thinking_blocks = llm_result.get("thinking_blocks")
            if llm_usage:
                prompt_tokens += llm_usage.get("prompt_tokens", 0)
                completion_tokens += llm_usage.get("completion_tokens", 0)

            # Build assistant message dict for history.
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

            # No tool calls — final text answer.
            if not llm_tool_calls:
                content = llm_content or "(No output)"
                log_entry.update(
                    status="completed", status_code=200,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    iterations=iteration, tool_calls=total_tool_calls,
                    llm_tokens={"prompt": prompt_tokens, "completion": completion_tokens},
                    tools_used=list(set(tools_used)), errors=[],
                )
                _save_task_log(log_entry)
                return 200, AgentOutput(content=content)

            # Process tool calls.
            for tc in llm_tool_calls:
                tool_name = tc["name"]
                arguments = tc.get("arguments", {})
                tc_id = tc["id"]

                total_tool_calls += 1
                tools_used.append(tool_name)

                if total_tool_calls > _MAX_TOOL_CALLS:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": f"Error: Maximum tool call limit ({_MAX_TOOL_CALLS}) reached. Please provide your final answer.",
                    })
                    continue

                # Handle fetch_agent_documentation locally.
                if tool_name == "fetch_agent_documentation":
                    from helper import handle_fetch_agent_documentation
                    doc_result = await handle_fetch_agent_documentation(
                        arguments.get("agent_id", ""), task_available_destinations,
                        agent_config.router_url,
                    )
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": doc_result})
                    continue

                # Handle sub-agent tools (call_{agent_id}).
                if tool_name.startswith("call_"):
                    dest_agent_id = tool_name[5:]
                    # Authoritative session-context injection: override any
                    # user_id/session_id/timezone the LLM generated so that
                    # downstream ACL (llm_agent allowed_models, per-user
                    # model maps) sees the real session owner.
                    dest_info = task_available_destinations.get(dest_agent_id, {})
                    dest_schema = dest_info.get("input_schema", "")
                    sub_payload = dict(arguments)
                    if "user_id" in dest_schema:
                        sub_payload["user_id"] = user_id
                    if "session_id" in dest_schema:
                        sub_payload["session_id"] = session_id
                    if user_tz and user_tz != "UTC":
                        if "timezone" in dest_schema or "session_id" in dest_schema:
                            sub_payload["timezone"] = user_tz
                    try:
                        identifier = f"sub_{uuid.uuid4().hex[:12]}"
                        loop = asyncio.get_running_loop()
                        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
                        _pending_sub[identifier] = fut
                        try:
                            await router_client.spawn(
                                identifier=identifier,
                                parent_task_id=task_id,
                                destination_agent_id=dest_agent_id,
                                payload=sub_payload,
                            )
                            sub_result = await asyncio.wait_for(fut, timeout=120.0)
                            sub_payload_result = sub_result.get("payload", {})
                            result_text = sub_payload_result.get("content", "") or json.dumps(sub_payload_result)
                            # Note any attached files the sub-agent returned.
                            sub_files = sub_payload_result.get("files") or []
                            if sub_files:
                                names = ", ".join(
                                    f.get("original_filename") or Path(f.get("path", "")).name or "?"
                                    for f in sub_files
                                )
                                result_text += (
                                    f"\n[{len(sub_files)} file(s) attached by sub-agent: {names}."
                                    " You cannot open or process these files directly;"
                                    " relay their existence to the user.]"
                                )
                        finally:
                            _pending_sub.pop(identifier, None)
                    except asyncio.TimeoutError:
                        result_text = f"Sub-agent '{dest_agent_id}' timed out"
                    except Exception as e:
                        result_text = f"Error calling sub-agent '{dest_agent_id}': {e}"

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": result_text,
                    })
                    continue

                # Handle local tools.
                result = await engine.execute(tool_name, arguments)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result,
                })

        # Max iterations reached.
        last_content = ""
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                last_content = msg["content"]
                break

        content = (
            f"Warning: Maximum iterations ({_MAX_ITERATIONS}) reached.\n\n"
            f"Last output:\n{last_content}" if last_content else
            f"Warning: Maximum iterations ({_MAX_ITERATIONS}) reached."
        )
        log_entry.update(
            status="max_iterations", status_code=200,
            finished_at=datetime.now(timezone.utc).isoformat(),
            iterations=iteration, tool_calls=total_tool_calls,
            errors=["max_iterations_reached"],
        )
        _save_task_log(log_entry)
        return 200, AgentOutput(content=content)

    except Exception as e:
        logger.exception("[%s] Unrecoverable error in agent loop", task_id)
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

_AGENT_DOC_PATH = Path(__file__).parent / "AGENT.md"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize agent on startup."""
    global agent_config, agent_info, router_client, reminder_db, available_destinations, _checker_task

    agent_config = AgentConfig.from_env()

    # Ensure directories exist.
    Path(agent_config.data_dir).mkdir(parents=True, exist_ok=True)
    Path(agent_config.log_dir).mkdir(parents=True, exist_ok=True)

    # Initialize database.
    users_dir = Path(agent_config.data_dir) / "users"
    reminder_db = ReminderDB(users_dir)

    # Credential persistence.
    creds_path = Path(agent_config.credentials_path)
    creds_path.parent.mkdir(parents=True, exist_ok=True)

    saved_creds: dict[str, str] = {}
    if creds_path.exists():
        try:
            saved_creds = json.loads(creds_path.read_text())
            logger.info("Loaded saved credentials for '%s'", saved_creds.get("agent_id"))
        except Exception:
            pass

    # Documentation URL for agent onboarding.
    doc_url = f"file://{_AGENT_DOC_PATH}" if _AGENT_DOC_PATH.exists() else None

    agent_info = AgentInfo(
        agent_id=agent_config.agent_id,
        description=(
            "Reminder and schedule agent. Manages calendar events, tasks, and reminders "
            "via natural language. Provide instructions in llmdata.prompt with context "
            "in llmdata.context. Sends proactive notifications for due items."
        ),
        input_schema="llmdata: LLMData, user_id: str, session_id: str, timezone: Optional[str]",
        output_schema="content: str",
        required_input=["llmdata", "user_id", "session_id"],
        documentation_url=doc_url,
    )

    _agent_url = agent_config.agent_url or f"http://localhost:{agent_config.agent_port}"
    _endpoint_url = f"{_agent_url}/receive"

    if saved_creds.get("auth_token"):
        agent_config.agent_auth_token = saved_creds["auth_token"]
        agent_config.agent_id = saved_creds.get("agent_id", agent_config.agent_id)
        router_client = RouterClient(
            router_url=agent_config.router_url,
            agent_id=agent_config.agent_id,
            auth_token=agent_config.agent_auth_token,
        )
        try:
            await router_client.refresh_from_agent_info(agent_info, endpoint_url=_endpoint_url)
        except Exception as e:
            logger.warning("Failed to refresh agent info: %s", e)
        try:
            dest_data = await router_client.get_destinations()
            available_destinations = dest_data.get("available_destinations", {})
        except Exception as e:
            logger.warning("Failed to fetch destinations: %s", e)
        logger.info("Using saved auth token for agent '%s'.", agent_config.agent_id)

    elif agent_config.invitation_token:
        logger.info("Onboarding with router using invitation token...")
        try:
            resp = await onboard(
                router_url=agent_config.router_url,
                invitation_token=agent_config.invitation_token,
                endpoint_url=_endpoint_url,
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
            logger.info(
                "Onboarded as '%s'. Credentials saved. Destinations: %s",
                resp.agent_id, list(available_destinations.keys()),
            )
        except Exception as e:
            logger.error("Failed to onboard: %s. Agent will start without router connection.", e)
            router_client = None  # type: ignore[assignment]
    else:
        logger.warning("No saved credentials or invitation token. Agent will start without router connection.")
        router_client = None  # type: ignore[assignment]

    # Start periodic checker if router is connected.
    if router_client:
        from checker import _parse_hours_list
        _checker_task = asyncio.create_task(
            periodic_check_loop(
                db=reminder_db,
                router_client=router_client,
                core_agent_id=agent_config.core_agent_id,
                check_interval=agent_config.check_interval,
                event_notify_hours=_parse_hours_list(agent_config.event_notify_hours),
                task_notify_hours=_parse_hours_list(agent_config.task_notify_hours),
                urgent_task_notify_hours=_parse_hours_list(agent_config.urgent_task_notify_hours),
                log_dir=agent_config.log_dir,
            )
        )
        logger.info("Periodic checker started (interval: %d min).", agent_config.check_interval)

    logger.info("Reminder agent started on %s:%d", agent_config.agent_host, agent_config.agent_port)
    yield

    # Shutdown.
    if _checker_task:
        _checker_task.cancel()
        try:
            await _checker_task
        except asyncio.CancelledError:
            pass
    if router_client:
        await router_client.aclose()
    pass


app = FastAPI(title="Reminder Agent", lifespan=lifespan)


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
        _au = agent_config.agent_url or f"http://localhost:{agent_config.agent_port}"
        await router_client.refresh_from_agent_info(agent_info, endpoint_url=f"{_au}/receive")
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
        # Check if a pending future is waiting for this result.
        pending_fut = None
        if identifier:
            pending_fut = _pending_llm.get(identifier) or _pending_sub.get(identifier)
        if pending_fut and not pending_fut.done():
            pending_fut.set_result(body)
        else:
            # Fire-and-forget spawn result (e.g. checker notification) — just log it.
            sc = body.get("status_code", 200)
            logger.info("Result delivery [%s] status=%s (no pending future)", identifier, sc)
        return JSONResponse({"status": "accepted"}, status_code=202)

    # Update available_destinations if provided.
    global available_destinations
    task_destinations = body.get("available_destinations") or available_destinations
    if "available_destinations" in body:
        available_destinations = body["available_destinations"]

    # Extract LLMData.
    raw_llmdata = payload.get("llmdata")
    if not raw_llmdata:
        logger.error("[%s] No llmdata in payload", task_id)
        if router_client:
            result = build_result_request(
                agent_id=agent_config.agent_id,
                task_id=task_id,
                parent_task_id=parent_task_id,
                status_code=400,
                output=AgentOutput(content="Error: No llmdata provided. This agent requires llmdata with a prompt."),
            )
            await router_client.route(result)
        return JSONResponse({"status": "error", "message": "Missing llmdata"}, status_code=400)

    llmdata = LLMData.model_validate(raw_llmdata)
    user_id = str(payload.get("user_id") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()

    if not user_id:
        logger.error("[%s] No user_id in payload", task_id)
        if router_client:
            result = build_result_request(
                agent_id=agent_config.agent_id,
                task_id=task_id,
                parent_task_id=parent_task_id,
                status_code=400,
                output=AgentOutput(content="Error: user_id is required."),
            )
            await router_client.route(result)
        return JSONResponse({"status": "error", "message": "Missing user_id"}, status_code=400)

    if not session_id:
        logger.error("[%s] No session_id in payload", task_id)
        if router_client:
            result = build_result_request(
                agent_id=agent_config.agent_id,
                task_id=task_id,
                parent_task_id=parent_task_id,
                status_code=400,
                output=AgentOutput(content="Error: session_id is required."),
            )
            await router_client.route(result)
        return JSONResponse({"status": "error", "message": "Missing session_id"}, status_code=400)

    # Capture reporting identifiers if not set.
    origin_agent_id = body.get("agent_id")  # the agent that called us
    await reminder_db.ensure_reporting_session(user_id, session_id, origin_agent_id)

    # Update timezone if provided in payload.
    payload_tz = payload.get("timezone")
    if payload_tz:
        await reminder_db.update_settings(user_id, {"timezone": payload_tz})

    logger.info("[%s] Received task for user '%s': %s", task_id, user_id, llmdata.prompt[:100])

    # Run agent loop in background.
    asyncio.create_task(_process_task(task_id, parent_task_id, llmdata, user_id, session_id, task_destinations))

    return JSONResponse({"status": "accepted", "task_id": task_id})


async def _process_task(
    task_id: str,
    parent_task_id: Optional[str],
    llmdata: LLMData,
    user_id: str,
    session_id: str,
    task_destinations: dict[str, Any],
) -> None:
    """Process a task and report result back to router."""
    status_code, output = await run_agent_loop(
        task_id, parent_task_id, llmdata, user_id, session_id, task_destinations,
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
            resp = await router_client.route(result)
            logger.info("[%s] Result reported to router: %s", task_id, resp.status_code)
        except Exception as e:
            logger.error("[%s] Failed to report result: %s", task_id, e)
    else:
        logger.warning("[%s] No router connection, result not reported.", task_id)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "agent_id": agent_config.agent_id if agent_config else "not initialized",
        "router_connected": router_client is not None,
        "checker_running": _checker_task is not None and not _checker_task.done(),
    })


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

from web_ui import create_ui_router
from fastapi.staticfiles import StaticFiles

app.include_router(create_ui_router())

_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static-ui")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    from dotenv import load_dotenv
    env_path = Path(__file__).parent / "data" / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    config = AgentConfig.from_env()
    uvicorn.run(
        "agent:app",
        host=config.agent_host,
        port=config.agent_port,
        reload=False,
    )
