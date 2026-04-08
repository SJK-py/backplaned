"""
kb_agent/agent.py — Knowledge Base management agent.

External agent with two interaction modes:
- LLM-driven: receives LLMData + user_id, uses tools to manage knowledge base
- Web UI: users and admins manage documents directly

Supports: document storage, hybrid search, metadata management, file operations.
Documents are converted to markdown via md_converter before storage.
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
    handle_fetch_agent_documentation,
    onboard,
)

from config import AgentConfig
from db import KnowledgeDB
from tools import KB_TOOLS

load_dotenv(Path(__file__).parent / "data" / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("kb_agent")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

agent_config: AgentConfig = None  # type: ignore[assignment]
agent_info: AgentInfo = None  # type: ignore[assignment]
router_client: RouterClient = None  # type: ignore[assignment]
kb_db: KnowledgeDB = None  # type: ignore[assignment]
available_destinations: dict[str, Any] = {}

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

    try:
        await router_client.spawn(
            identifier=identifier,
            parent_task_id=None,
            destination_agent_id=agent_config.llm_agent_id,
            payload={"llmcall": llmcall_payload, "user_id": user_id or "not_specified"},
        )
        result_data = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError("LLM call timed out")
    finally:
        _pending_results.pop(identifier, None)

    raw = result_data.get("payload", {})
    sc = result_data.get("status_code", 200)
    content_str = raw.get("content", "")
    if sc and sc >= 400:
        raise RuntimeError(f"llm_agent error ({sc}): {content_str}")

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
    timeout: float = 120.0,
) -> dict[str, Any]:
    identifier = f"tc_{uuid.uuid4().hex[:12]}"
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = loop.create_future()
    _pending_results[identifier] = fut
    try:
        await router_client.spawn(
            identifier=identifier,
            parent_task_id=None,
            destination_agent_id=dest_agent_id,
            payload=payload,
        )
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"Sub-agent '{dest_agent_id}' timed out")
    finally:
        _pending_results.pop(identifier, None)


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


def _resolve_workspace_path(fp: str, workspace: Path) -> Optional[Path]:
    """Resolve a file path confined to the workspace. Returns None if path escapes."""
    try:
        if Path(fp).is_absolute():
            resolved = Path(fp).resolve()
        else:
            resolved = (workspace / fp).resolve()
        resolved.relative_to(workspace.resolve())
        return resolved
    except (ValueError, OSError):
        return None


async def _execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    user_id: str,
    workspace: Path,
    pfm: ProxyFileManager,
) -> str:
    """Execute a knowledge base tool and return result string."""

    if tool_name == "store_document":
        fp = arguments.get("file_path", "")
        resolved = _resolve_workspace_path(fp, workspace)
        if not resolved or not resolved.exists():
            return f"Error: file not found or access denied: {fp}"
        text = resolved.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            return "Error: file is empty"
        title = arguments.get("title") or resolved.stem
        tags = [t.strip() for t in arguments.get("tags", "").split(",") if t.strip()] if arguments.get("tags") else None
        result = await kb_db.store_document(
            user_id=user_id,
            text=text,
            title=title,
            collection=arguments.get("collection", "default"),
            description=arguments.get("description", ""),
            date=arguments.get("date", ""),
            tags=tags,
        )
        return json.dumps(result, indent=2)

    if tool_name == "search_knowledge":
        results = await kb_db.search(
            user_id=user_id,
            query=arguments.get("query", ""),
            count=arguments.get("count", 5),
            collection=arguments.get("collection"),
            title=arguments.get("title"),
            tag=arguments.get("tag"),
            mode=arguments.get("mode", "hybrid"),
        )
        if not results:
            return "No results found."
        return json.dumps(results, indent=2, ensure_ascii=False)

    if tool_name == "list_documents":
        docs = await kb_db.list_documents(user_id)
        if not docs:
            return "No documents in knowledge base."
        return json.dumps(docs, indent=2, ensure_ascii=False)

    if tool_name == "remove_document":
        removed = await kb_db.remove_document(user_id, arguments.get("title", ""))
        return "Document removed." if removed else "Document not found."

    if tool_name == "modify_document_metadata":
        tags = [t.strip() for t in arguments.get("tags", "").split(",") if t.strip()] if arguments.get("tags") else None
        ok = await kb_db.modify_metadata(
            user_id=user_id,
            title=arguments.get("title", ""),
            collection=arguments.get("collection"),
            description=arguments.get("description"),
            date=arguments.get("date"),
            tags=tags,
        )
        return "Metadata updated." if ok else "Failed to update metadata."

    if tool_name == "read_file":
        fp = arguments.get("file_path", "")
        resolved = _resolve_workspace_path(fp, workspace)
        if not resolved:
            return "Error: access denied — file must be in workspace."
        if not resolved.is_file():
            return f"Error: file not found: {fp}"
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
            if len(text) > 50000:
                text = text[:50000] + "\n\n[Truncated]"
            return text
        except Exception as e:
            return f"Error: {e}"

    if tool_name == "write_file":
        fp = arguments.get("file_path", "")
        resolved = _resolve_workspace_path(fp, workspace)
        if not resolved:
            return "Error: access denied — file must be in workspace."
        content = arguments.get("content", "")
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
            return f"File written: {fp}"
        except Exception as e:
            return f"Error: {e}"

    if tool_name == "list_files":
        fp = arguments.get("file_path", "")
        resolved = _resolve_workspace_path(fp, workspace) if fp else workspace
        if not resolved:
            return "Error: access denied — path must be in workspace."
        if not resolved.is_dir():
            return f"Error: not a directory: {fp}"
        try:
            entries = sorted(resolved.iterdir())
            lines = []
            for e in entries:
                try:
                    rel = str(e.relative_to(workspace))
                except ValueError:
                    rel = e.name
                lines.append(f"{'[DIR]' if e.is_dir() else f'{e.stat().st_size:>8d}'} {rel}")
            return "\n".join(lines) if lines else "(empty directory)"
        except Exception as e:
            return f"Error: {e}"

    if tool_name == "delete_file":
        fp = arguments.get("file_path", "")
        resolved = _resolve_workspace_path(fp, workspace)
        if not resolved:
            return "Error: access denied — file must be in workspace."
        try:
            resolved.unlink()
            return f"Deleted: {fp}"
        except Exception as e:
            return f"Error: {e}"

    return f"Unknown tool: {tool_name}"


# ---------------------------------------------------------------------------
# Per-user config
# ---------------------------------------------------------------------------


def _get_user_model_id(user_id: str) -> Optional[str]:
    """Look up per-user model_id from users.json."""
    users_file = Path(agent_config.data_dir) / "users.json"
    if users_file.exists():
        try:
            users = json.loads(users_file.read_text())
            user = users.get(user_id, {})
            mid = user.get("model_id", "")
            if mid:
                return mid
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


async def run_agent_loop(
    task_id: str,
    parent_task_id: Optional[str],
    llmdata: LLMData,
    user_id: str,
    files: list[dict[str, Any]],
) -> tuple[int, AgentOutput]:
    """Run the LLM tool-calling loop."""

    workspace = Path(agent_config.data_dir) / "workspaces" / user_id
    workspace.mkdir(parents=True, exist_ok=True)

    pfm = ProxyFileManager(
        inbox_dir=workspace / "inbox",
        router_url=agent_config.router_url,
        agent_endpoint_url=agent_config.agent_endpoint_url or f"http://localhost:{agent_config.agent_port}",
    )

    # Download inbound files to workspace
    local_paths: list[str] = []
    for f in files:
        try:
            logger.info("Fetching file: %s (protocol=%s)", f.get("path"), f.get("protocol"))
            lp = await pfm.fetch(f, task_id)
            local_paths.append(lp)
            logger.info("Fetched to: %s", lp)
        except Exception as exc:
            logger.error("Failed to fetch file %s: %s", f.get("path"), exc, exc_info=True)

    # Build system prompt
    system_prompt = (
        "You are a knowledge base agent in a multi-agent system. You store, "
        "search, and manage documents in per-user vector databases.\n\n"
        "## Capabilities\n"
        "- Store .md files (chunked + embedded for hybrid search)\n"
        "- Search via semantic + full-text hybrid search\n"
        "- List, remove, and modify document metadata\n"
        "- Read/write/list/delete files in the user's workspace\n"
        "- Convert non-markdown files via call_md_converter\n\n"
        "## Non-markdown file workflow\n"
        "1. Call call_md_converter with file and output_file=true\n"
        "2. The converted .md file appears in [Result files:] — read it with read_file\n"
        "3. Store the content using store_document\n\n"
        "## File handling\n"
        "- Your workspace is PRIVATE. No other agent can access it.\n"
        "- All file operations use the file_path argument. NEVER embed paths in prompt text.\n"
        "- Inbound files from the caller arrive in the workspace inbox automatically.\n"
        "- When calling other agents, pass files via the files argument (auto-transferred). "
        "NEVER reference your workspace paths in prompts to other agents.\n"
        "- If you need an agent to produce a file, explicitly ask it to attach the result.\n"
        "- Result files from sub-agents appear in [Result files:] blocks with file_path.\n\n"
        "## Rules\n"
        "- Only .md text can be stored in the database\n"
        "- Provide meaningful title, description, and tags when storing\n"
        "- Your result goes to another agent — include enough detail for the caller "
        "to understand what was stored, found, or modified.\n"
        f"\nUser: {user_id}\nWorkspace: {workspace}"
    )
    if llmdata.agent_instruction:
        system_prompt += f"\n\n[Caller Instruction]\n{llmdata.agent_instruction}"
    if llmdata.context:
        system_prompt += f"\n\n[Caller Context]\n{llmdata.context}"
    # Current time at the end of system prompt (after caller sections) to
    # maximise prompt-cache hit rate on the static prefix.
    _now = datetime.now(timezone.utc)
    system_prompt += f"\n\nCurrent time: {_now.strftime('%Y-%m-%d %H:%M %Z')} ({_now.strftime('%A')})"

    # File info injected into user message (not system prompt) for prompt caching
    user_content = llmdata.prompt
    if local_paths:
        file_lines = []
        for p in local_paths:
            fname = Path(p).name
            try:
                display = str(Path(p).relative_to(workspace))
            except ValueError:
                display = p
            ext = Path(p).suffix.lower()
            if ext in (".pdf", ".docx", ".pptx", ".xlsx", ".doc", ".ppt"):
                file_lines.append(f'  - {fname} (file_path: "{display}") — document file, convert with call_md_converter before storing')
            else:
                file_lines.append(f'  - {fname} (file_path: "{display}")')
        user_content += "\n\n[Attached files:\n" + "\n".join(file_lines) + "\n]"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    # Build tools: local KB tools + remote agent tools
    remote_tools = build_openai_tools(available_destinations) if available_destinations else []
    all_tools = KB_TOOLS + remote_tools

    iteration = 0
    total_tool_calls = 0
    prompt_tokens = 0
    completion_tokens = 0

    while iteration < _MAX_ITERATIONS:
        iteration += 1

        user_model = _get_user_model_id(user_id) or agent_config.default_model_id or None
        try:
            llm_result = await _llm_call(
                messages, all_tools,
                timeout=agent_config.tool_timeout,
                model_id=user_model,
                user_id=user_id,
            )
        except RuntimeError as exc:
            return 504, AgentOutput(content=f"LLM call failed: {exc}")

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
                {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                for tc in llm_tool_calls
            ]
        if llm_thinking_blocks:
            assistant_dict["thinking_blocks"] = llm_thinking_blocks
        messages.append(assistant_dict)

        if not llm_tool_calls:
            return 200, AgentOutput(content=llm_content or "(No output)")

        for tc in llm_tool_calls:
            tool_name = tc["name"]
            arguments = tc.get("arguments", {})
            tc_id = tc["id"]
            total_tool_calls += 1

            if total_tool_calls > _MAX_TOOL_CALLS:
                messages.append({"role": "tool", "tool_call_id": tc_id, "content": "Error: tool call limit reached."})
                continue

            # fetch_agent_documentation
            if tool_name == "fetch_agent_documentation":
                doc_result = await handle_fetch_agent_documentation(
                    arguments.get("agent_id", ""), available_destinations, agent_config.router_url,
                )
                messages.append({"role": "tool", "tool_call_id": tc_id, "content": doc_result})
                continue

            # Sub-agent tools
            if tool_name.startswith("call_"):
                dest = tool_name[5:]
                try:
                    result_data = await _spawn_and_wait(
                        dest, pfm.resolve_in_args(arguments), timeout=agent_config.tool_timeout,
                    )
                    result_text = await extract_result_text(result_data, pfm, path_display_base=workspace)
                    sc = result_data.get("status_code")
                    if sc and sc >= 400:
                        result_text = f"Agent '{dest}' error ({sc}): {result_text}"
                except RuntimeError as exc:
                    result_text = f"Agent '{dest}' failed: {exc}"
                messages.append({"role": "tool", "tool_call_id": tc_id, "content": result_text})
                continue

            # Local KB tools
            result = await _execute_tool(tool_name, arguments, user_id, workspace, pfm)
            messages.append({"role": "tool", "tool_call_id": tc_id, "content": result})

    return 200, AgentOutput(content=llm_content or "Max iterations reached.")


# ---------------------------------------------------------------------------
# Task processing
# ---------------------------------------------------------------------------


async def _process_task(
    task_id: str,
    parent_task_id: Optional[str],
    llmdata: LLMData,
    user_id: str,
    files: list[dict[str, Any]],
) -> None:
    status_code, output = await run_agent_loop(task_id, parent_task_id, llmdata, user_id, files)
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
    global agent_config, agent_info, router_client, kb_db, available_destinations

    agent_config = AgentConfig.from_env()

    Path(agent_config.data_dir).mkdir(parents=True, exist_ok=True)

    kb_db = KnowledgeDB(
        base_dir=Path(agent_config.data_dir) / "lancedb",
        embed_base_url=agent_config.embed_base_url,
        embed_api_key=agent_config.embed_api_key,
        embed_model=agent_config.embed_model,
        embed_timeout=agent_config.embed_timeout,
        vector_dim=agent_config.vector_dim,
        chunk_max=agent_config.chunk_len_max,
        chunk_min=agent_config.chunk_len_min,
        chunk_overlap=agent_config.chunk_overlap,
    )

    # Credentials
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
            "Knowledge base agent. Stores, searches, and manages markdown documents "
            "with hybrid search (vector + full-text). Provide instructions in "
            "llmdata.prompt. Pass files via the files argument for ingestion."
        ),
        input_schema="llmdata: LLMData, user_id: str, files: Optional[List[ProxyFile]]",
        output_schema="content: str",
        required_input=["llmdata", "user_id"],
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

    yield

    if router_client:
        await router_client.aclose()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Knowledge Base Agent", lifespan=lifespan)

_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


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
    # Reject symlinks to prevent path escape
    if Path(local_path).is_symlink():
        return JSONResponse({"error": "Symlinks not allowed"}, status_code=403)
    return FileResponse(local_path)


@app.post("/refresh-info")
async def refresh_info(request: Request) -> JSONResponse:
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


@app.post("/receive")
async def receive_task(request: Request) -> JSONResponse:
    # Verify delivery auth from router
    if agent_config.agent_auth_token:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not secrets.compare_digest(auth[7:], agent_config.agent_auth_token):
            return JSONResponse(status_code=403, content={"error": "Forbidden"})

    body = await request.json()
    task_id = body.get("task_id", "unknown")
    parent_task_id = body.get("parent_task_id")
    payload = body.get("payload", {})

    # Handle result deliveries
    identifier = body.get("identifier")
    dest = body.get("destination_agent_id")
    if dest is None and "status_code" in body:
        if identifier and identifier in _pending_results:
            fut = _pending_results.get(identifier)
            if fut and not fut.done():
                fut.set_result(body)
        return JSONResponse({"status": "accepted"}, status_code=202)

    # New task
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
    if not user_id:
        if router_client:
            result = build_result_request(
                agent_id=agent_config.agent_id,
                task_id=task_id,
                parent_task_id=parent_task_id,
                status_code=400,
                output=AgentOutput(content="Error: user_id is required."),
            )
            await router_client.route(result)
        return JSONResponse({"status": "error"}, status_code=400)

    files = payload.get("files", [])

    # Update available_destinations
    global available_destinations
    if "available_destinations" in body:
        available_destinations = body["available_destinations"]

    asyncio.create_task(_process_task(task_id, parent_task_id, llmdata, user_id, files))
    return JSONResponse({"status": "accepted"}, status_code=202)


# ---------------------------------------------------------------------------
# Web UI
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
