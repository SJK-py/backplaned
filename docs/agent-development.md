# Agent Development Guide

This guide walks through building a new agent for Backplaned. Agents are either **embedded** (run in-process with the router) or **external** (separate processes communicating over HTTP).

## Choosing Embedded vs External

| | Embedded | External |
|---|---|---|
| **Deployment** | Loaded by router at startup | Separate process, can be on a different host |
| **Communication** | Zero-latency ASGI transport | HTTP POST to `/receive` |
| **File access** | Shares router filesystem (`localfile` protocol) | Serves files over HTTP |
| **Isolation** | Shares router process (no resource limits) | Separate process (can have resource limits, sandboxing) |
| **Registration** | Auto-registered on startup | Requires invitation token onboarding |
| **Best for** | Stateless tools, lightweight agents | Stateful agents, sandboxed execution, agents needing resource isolation |

## Shared Library: helper.py

All agents import from `helper.py`, which provides models, message builders, and the `RouterClient`:

### Data Models

**ProxyFile** — Represents a file in the router's proxy system:
```python
class ProxyFile(BaseModel):
    path: str                    # Logical path or URL
    protocol: Literal["router-proxy", "http", "localfile"]
    key: Optional[str] = None   # Per-file access key
    original_filename: Optional[str] = None
```

**AgentInfo** — Metadata published to the router for discovery:
```python
class AgentInfo(BaseModel):
    agent_id: str
    description: str             # Capability description (used in LLM tool generation)
    input_schema: str            # Type annotations, e.g. "prompt: str, file: ProxyFile"
    output_schema: str           # Return type annotations
    required_input: list[str]    # Names of mandatory fields
    hidden: bool = False         # Exclude from LLM tool generation
    documentation_url: Optional[str] = None  # URL to markdown docs
```

**LLMData** — High-level prompt for LLM-backed agents:
```python
class LLMData(BaseModel):
    agent_instruction: Optional[str] = None  # System-level instruction
    context: Optional[str] = None            # Background context
    prompt: str                              # User-facing prompt
```

**LLMCall** — Raw LLM inference request (sent to llm_agent):
```python
class LLMCall(BaseModel):
    messages: list[dict[str, Any]]        # OpenAI chat-completions format
    tools: list[dict[str, Any]] = []      # OpenAI function-tool format
    tool_choice: Optional[Any] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    model_id: Optional[str] = None        # Model config key (default: "default")
```

**AgentOutput** — Standardized return value:
```python
class AgentOutput(BaseModel):
    content: Optional[str] = None
    files: Optional[list[ProxyFile]] = None
```

### Message Builders

```python
# Spawn a new task
build_spawn_request(agent_id, identifier, parent_task_id, destination_agent_id, payload)

# Report a result
build_result_request(agent_id, task_id, parent_task_id, status_code, output)

# Delegate a task
build_delegation_payload(agent_id, task_id, parent_task_id, destination_agent_id, llmdata, files, handoff_note)
```

### LLM Tool Builders

```python
# Generate tool definitions from available_destinations
build_openai_tools(available_destinations)     # OpenAI function-calling format
build_anthropic_tools(available_destinations)  # Anthropic tool format

# Fetch agent documentation (called when LLM uses fetch_agent_documentation tool)
await handle_fetch_agent_documentation(agent_id, available_destinations, router_url)
```

These functions parse `AgentInfo.input_schema` into JSON Schema and create tool definitions named `call_{agent_id}`. If any destination has documentation, a `fetch_agent_documentation` tool is also generated.

### RouterClient

Async client for agent-to-router communication:

```python
client = RouterClient(router_url="http://localhost:8000", agent_id="my_agent", auth_token="...")

# Spawn a new task
await client.spawn(identifier, parent_task_id, destination_agent_id, payload)

# Report result
await client.report_result(task_id, parent_task_id, status_code, output)

# Delegate task
await client.delegate(task_id, parent_task_id, destination_agent_id, llmdata, files, handoff_note)

# Send arbitrary routing payload
await client.route(payload)

# Push progress event
await client.push_progress(task_id, event_type, content, metadata)

# Refresh agent metadata
await client.refresh_from_agent_info(agent_info, endpoint_url)

# Fetch current destinations
destinations = await client.get_destinations()

# Cleanup
await client.aclose()
```

### ProxyFileManager

Handles file transfer between the agent and router:

```python
pfm = ProxyFileManager(
    inbox_dir="data/inbox",        # Local directory for downloaded files
    router_url="http://localhost:8000",
    agent_url="http://localhost:8086",  # This agent's public URL (None for embedded)
)

# Fetch a ProxyFile to local disk
local_path = await pfm.fetch(proxy_file_dict, task_id)

# Convert local path back to ProxyFile
proxy_file = pfm.resolve(local_path)

# Scan tool arguments for file paths and convert to ProxyFile dicts
resolved_args = pfm.resolve_in_args(tool_call_arguments)
```

### Onboarding

```python
from helper import onboard, OnboardRequest

response = await onboard(
    router_url="http://localhost:8000",
    invitation_token="abc123...",
    endpoint_url="http://localhost:8086/receive",
    agent_info=my_agent_info,
)
# response.agent_id, response.auth_token, response.available_destinations
```

---

## Building an Embedded Agent

### Directory Structure

```
agents/my_agent/
├── agent.py              # Required: FastAPI app + AGENT_INFO
├── config.default.json   # Default runtime config
├── config.example        # Config field descriptions (JSON)
└── data/                 # Runtime data (gitignored)
    └── config.json       # Active config (created from defaults)
```

### Minimal agent.py

```python
"""My embedded agent."""
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Import from the project root
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from helper import AgentInfo, AgentOutput

# ── Agent metadata ─────────────────────────────────────────────
AGENT_INFO = AgentInfo(
    agent_id="my_agent",
    description="Does something useful. Provide your request in the prompt field.",
    input_schema="prompt: str, option: Optional[bool]",
    output_schema="content: str",
    required_input=["prompt"],
)

# ── Configuration ──────────────────────────────────────────────
_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _DIR / "data" / "config.json"

def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        return json.loads(_CONFIG_PATH.read_text())
    default = _DIR / "config.default.json"
    if default.exists():
        return json.loads(default.read_text())
    return {}

# ── FastAPI app ────────────────────────────────────────────────
app = FastAPI()

@app.post("/receive")
async def receive(request: Request) -> JSONResponse:
    body = await request.json()
    payload = body.get("payload", {})
    task_id = body.get("task_id", "unknown")

    # Extract inputs
    prompt = payload.get("prompt", "")
    option = payload.get("option", False)

    # Do work...
    result_text = f"Processed: {prompt}"

    # Return result — the router processes the 200 response body
    # as a routing payload and delivers it to the origin agent
    from helper import build_result_request
    return JSONResponse(
        build_result_request(
            agent_id="my_agent",
            task_id=task_id,
            parent_task_id=body.get("parent_task_id"),
            status_code=200,
            output=AgentOutput(content=result_text),
        )
    )
```

**Key points for embedded agents:**
- The module must expose `app` (a FastAPI instance) and `AGENT_INFO` (an `AgentInfo` instance)
- The `POST /receive` endpoint receives task payloads
- Return a 200 response with a routing payload to send the result back
- The auth token is injected as `{AGENT_ID_UPPER}_AUTH_TOKEN` environment variable after module load (read it lazily, not at import time)
- The router calls the embedded agent via `httpx.ASGITransport` — no network I/O

---

## Building an External Agent

### Directory Structure

```
agents_external/my_agent/
├── agent.py              # Main application
├── config.py             # Configuration loading
├── config.default.json   # Default runtime config
├── config.example        # Config field descriptions (JSON)
├── .env.example          # Environment variable template
├── start.sh              # Startup script (optional)
├── AGENT.md              # Agent documentation (optional, served to other agents)
├── web_ui.py             # Admin UI (optional)
├── static/               # Web UI static files (optional)
└── data/                 # Runtime data (gitignored)
    ├── .env              # Active environment (created by start.sh)
    ├── config.json       # Active config
    └── credentials.json  # Saved router credentials
```

### Minimal agent.py

```python
"""My external agent."""
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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from helper import (
    AgentInfo, AgentOutput, LLMData, RouterClient,
    build_result_request, onboard,
)

logger = logging.getLogger("my_agent")

# ── Configuration ──────────────────────────────────────────────

ROUTER_URL = os.environ.get("ROUTER_URL", "http://localhost:8000")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "8090"))
AGENT_URL = os.environ.get("AGENT_URL", f"http://localhost:{AGENT_PORT}")
INVITATION_TOKEN = os.environ.get("INVITATION_TOKEN", "")
AUTH_TOKEN = os.environ.get("AGENT_AUTH_TOKEN", "")
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
CREDS_PATH = DATA_DIR / "credentials.json"

# ── State ──────────────────────────────────────────────────────

router_client: Optional[RouterClient] = None
available_destinations: dict[str, Any] = {}
agent_id: str = "my_agent"

AGENT_INFO = AgentInfo(
    agent_id="my_agent",
    description="My external agent. Provide instructions in llmdata.prompt.",
    input_schema="llmdata: LLMData, user_id: str",
    output_schema="content: str",
    required_input=["llmdata", "user_id"],
)

# Futures for waiting on sub-agent results
_pending: dict[str, asyncio.Future] = {}

# ── Lifecycle ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global router_client, available_destinations, agent_id

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    endpoint_url = f"{AGENT_URL}/receive"

    # Try saved credentials first
    if CREDS_PATH.exists():
        try:
            creds = json.loads(CREDS_PATH.read_text())
            agent_id = creds["agent_id"]
            router_client = RouterClient(ROUTER_URL, agent_id, creds["auth_token"])
            await router_client.refresh_from_agent_info(AGENT_INFO, endpoint_url=endpoint_url)
            resp = await router_client.get_destinations()
            available_destinations = resp.get("available_destinations", {})
            logger.info("Reconnected as '%s'", agent_id)
        except Exception as e:
            logger.warning("Reconnect failed: %s", e)
            router_client = None

    # Otherwise onboard with invitation token
    if router_client is None and INVITATION_TOKEN:
        try:
            resp = await onboard(ROUTER_URL, INVITATION_TOKEN, endpoint_url, AGENT_INFO)
            agent_id = resp.agent_id
            router_client = RouterClient(ROUTER_URL, resp.agent_id, resp.auth_token)
            available_destinations = resp.available_destinations
            CREDS_PATH.write_text(json.dumps({
                "agent_id": resp.agent_id,
                "auth_token": resp.auth_token,
            }))
            logger.info("Onboarded as '%s'", agent_id)
        except Exception as e:
            logger.error("Onboarding failed: %s", e)

    yield

    if router_client:
        await router_client.aclose()

app = FastAPI(title="My Agent", lifespan=lifespan)

# ── Endpoints ──────────────────────────────────────────────────

@app.post("/receive")
async def receive_task(request: Request) -> JSONResponse:
    # Verify auth
    auth = request.headers.get("authorization", "")
    if AUTH_TOKEN and (not auth.startswith("Bearer ") or
                       not secrets.compare_digest(auth[7:], AUTH_TOKEN)):
        return JSONResponse(status_code=403, content={"error": "Forbidden"})

    body = await request.json()

    # Handle result deliveries (from sub-agent calls)
    identifier = body.get("identifier")
    if body.get("destination_agent_id") is None and "status_code" in body:
        fut = _pending.pop(identifier, None)
        if fut and not fut.done():
            fut.set_result(body)
        return JSONResponse({"status": "accepted"}, status_code=202)

    # Update destinations
    global available_destinations
    if "available_destinations" in body:
        available_destinations = body["available_destinations"]

    # Process task in background
    task_id = body.get("task_id", "unknown")
    parent_task_id = body.get("parent_task_id")
    payload = body.get("payload", {})

    asyncio.create_task(_process_task(task_id, parent_task_id, payload))
    return JSONResponse({"status": "accepted", "task_id": task_id}, status_code=202)

@app.get("/health")
async def health():
    return {"status": "ok", "agent_id": agent_id}

@app.post("/refresh-info")
async def refresh_info(request: Request):
    """Called by router to refresh agent metadata."""
    if router_client:
        endpoint_url = f"{AGENT_URL}/receive"
        await router_client.refresh_from_agent_info(AGENT_INFO, endpoint_url=endpoint_url)
    return {"status": "ok"}

# ── Task processing ────────────────────────────────────────────

async def _process_task(task_id: str, parent_task_id: Optional[str],
                        payload: dict[str, Any]) -> None:
    try:
        llmdata_raw = payload.get("llmdata", {})
        llmdata = LLMData.model_validate(llmdata_raw)
        user_id = payload.get("user_id", "")

        # Do your work here...
        result_text = f"Processed for {user_id}: {llmdata.prompt}"

        status_code = 200
        output = AgentOutput(content=result_text)
    except Exception as e:
        logger.exception("Task %s failed", task_id)
        status_code = 500
        output = AgentOutput(content=f"Error: {e}")

    if router_client:
        result = build_result_request(
            agent_id=agent_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=status_code,
            output=output,
        )
        await router_client.route(result)

# ── Calling sub-agents ─────────────────────────────────────────

async def _call_agent(dest_agent_id: str, payload: dict, parent_task_id: str,
                      timeout: float = 120.0) -> dict:
    """Spawn a sub-task and wait for the result."""
    identifier = f"sub_{uuid.uuid4().hex[:12]}"
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _pending[identifier] = fut

    await router_client.spawn(
        identifier=identifier,
        parent_task_id=parent_task_id,
        destination_agent_id=dest_agent_id,
        payload=payload,
    )

    return await asyncio.wait_for(fut, timeout=timeout)

# ── Entry point ────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    from dotenv import load_dotenv
    env_path = DATA_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    uvicorn.run("agent:app", host="0.0.0.0", port=AGENT_PORT)
```

### Result Waiting Pattern

External agents use asyncio Futures to wait for sub-agent results:

1. Agent spawns a sub-task with a unique `identifier`
2. Agent stores a Future keyed by that identifier
3. When the router delivers the result to `/receive`, the identifier is matched and the Future is resolved
4. The spawning coroutine unblocks via `await asyncio.wait_for(fut, timeout=...)`

```python
# Spawn and wait
identifier = f"llm_{uuid.uuid4().hex[:12]}"
fut = asyncio.get_running_loop().create_future()
_pending[identifier] = fut

await router_client.spawn(
    identifier=identifier,
    parent_task_id=task_id,
    destination_agent_id="llm_agent",
    payload={"llmcall": llmcall_dict, "user_id": user_id},
)

result = await asyncio.wait_for(fut, timeout=120.0)
# result["payload"]["content"] contains the LLM response
```

### LLM Agent Loop Pattern

Most agents follow a multi-turn tool-calling loop:

```python
from helper import build_openai_tools, handle_fetch_agent_documentation

async def run_agent_loop(task_id, llmdata, user_id, destinations):
    # Build tools from available destinations + local tools
    remote_tools = build_openai_tools(destinations)
    local_tools = [...]  # Your agent's own tool definitions
    all_tools = local_tools + remote_tools

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": llmdata.prompt},
    ]

    for iteration in range(MAX_ITERATIONS):
        # Call LLM via llm_agent
        llm_result = await _call_llm(messages, all_tools, task_id, user_id)
        content = llm_result.get("content", "")
        tool_calls = llm_result.get("tool_calls", [])

        if not tool_calls:
            return 200, AgentOutput(content=content)

        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})

        for tc in tool_calls:
            name = tc["function"]["name"]
            args = json.loads(tc["function"]["arguments"])
            tc_id = tc["id"]

            if name == "fetch_agent_documentation":
                result = await handle_fetch_agent_documentation(
                    args["agent_id"], destinations, ROUTER_URL
                )
            elif name.startswith("call_"):
                dest_id = name[5:]
                sub_result = await _call_agent(dest_id, args, task_id)
                result = sub_result.get("payload", {}).get("content", "")
            else:
                result = await execute_local_tool(name, args)

            messages.append({"role": "tool", "tool_call_id": tc_id, "content": result})

    return 200, AgentOutput(content="Max iterations reached.")
```

### Agent Documentation

Agents can provide detailed documentation as a Markdown file. The router stores it and makes it available to other agents via the `fetch_agent_documentation` tool.

To enable documentation:
1. Create an `AGENT.md` file in your agent directory
2. Set `documentation_url` in your `AgentInfo`:
   ```python
   AGENT_INFO = AgentInfo(
       ...,
       documentation_url=f"file://{Path(__file__).parent / 'AGENT.md'}",
   )
   ```
3. Other agents' LLMs will see a hint in your tool description and can fetch the docs before calling

### Config UI Integration

For external agents with a web UI, use the shared config_ui module:

```python
from config_ui import add_config_routes

add_config_routes(
    router=api_router,
    agent_dir=Path(__file__).parent,  # Must contain config.example
    require_auth=require_session_auth,
    cookie_name="my_agent_session",
)
# Provides GET/PUT /ui/config endpoints
```

### Registering with start.sh

To have `start.sh` automatically bootstrap and launch your external agent:

1. Add a `.env.example` with required variables
2. Add a `config.default.json` with default runtime config
3. Add a startup block in `start.sh` following the existing pattern
4. Add group assignments in the `start.sh` onboarding section

---

## Input Schema Format

The `input_schema` string in `AgentInfo` defines what arguments your agent accepts. The router parses it into JSON Schema for LLM tool generation.

**Format:** Comma-separated `name: type` pairs.

**Supported types:**

| Type | JSON Schema |
|------|-------------|
| `str` | `{"type": "string"}` |
| `int` | `{"type": "integer"}` |
| `bool` | `{"type": "boolean"}` |
| `float` | `{"type": "number"}` |
| `dict` | `{"type": "object"}` |
| `ProxyFile` | `{"type": "string", "description": "Local file path."}` |
| `LLMData` | Object with `agent_instruction`, `context`, `prompt` |
| `List[X]` | `{"type": "array", "items": <X>}` |
| `Optional[X]` | `{"oneOf": [<X>, {"type": "null"}]}` |

**Examples:**
```
"prompt: str, count: int"
"llmdata: LLMData, user_id: str, files: Optional[List[ProxyFile]]"
"file: ProxyFile, output_file: Optional[bool]"
"operation: str, content: str, user_id: str, count: Optional[int]"
```

Fields not wrapped in `Optional[...]` are added to the JSON Schema `required` list.

## Progress Events

Push progress events to keep clients informed during long-running tasks:

```python
# Event types: "thinking", "tool_call", "tool_result", "status", "chunk", "done"
await router_client.push_progress(
    task_id=task_id,
    event_type="status",
    content="Searching the web...",
    metadata={"step": 2, "total": 5},
)
```

Progress events are best-effort — failures are silently ignored and never block the main task.

## Error Handling

- Return `status_code >= 400` in your result to indicate failure
- The router marks the task as `failed` and propagates the error to the origin agent
- Use timeouts for all sub-agent calls (`asyncio.wait_for`)
- Cap iteration counts to prevent runaway loops
- Validate required inputs early and return clear error messages
