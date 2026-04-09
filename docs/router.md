# Router System Overview

The router is the central component of Backplaned. It acts as an ESB-style message broker that manages all inter-agent communication, task lifecycle, access control, file transfer, and agent registration.

**Stack:** FastAPI (asyncio), SQLite (WAL mode), httpx.

## Core Concepts

### Tasks

A **task** is the fundamental unit of work. Every interaction between agents is tracked as a task with a unique UUID.

**Task states:**

| State | Description |
|-------|-------------|
| `active` | Task is being processed |
| `completed` | Agent reported success (status_code < 400) |
| `failed` | Agent reported failure (status_code >= 400) or delivery error |
| `timeout` | Task exceeded `GLOBAL_TIMEOUT_HOURS` without completing |

**Task fields:**

| Field | Description |
|-------|-------------|
| `task_id` | Unique UUID |
| `parent_task_id` | Parent task (for nesting) |
| `identifier` | Caller's tracking string, re-injected when result is delivered back |
| `origin_agent_id` | Agent that spawned this task |
| `handler_agent_id` | Agent currently handling this task |
| `depth_count` | Nesting depth (capped by `MAX_DEPTH`) |
| `width_count` | Number of delegations (capped by `MAX_WIDTH`) |
| `timeout_at` | When this task will be auto-timed-out |

### Routing Operations

The router handles three types of routing payloads via `POST /route`:

#### 1. Spawn (task_id = "new")

Creates a new task targeting a destination agent.

- Generates a new task UUID
- Validates ACL permissions and depth/width limits
- Ingests any attached files into the proxy file vault
- Strips the `identifier` before forwarding (the router stores it and re-injects on result delivery)
- Injects `available_destinations` so the destination agent knows what it can call
- Delivers the payload to the destination agent

#### 2. Result (destination_agent_id = null)

Reports task completion back to the originating agent.

- Validates the sender is the current handler
- Updates task status to `completed` or `failed` based on `status_code`
- Re-injects the stored `identifier` from the original spawn
- Delivers the result to the origin agent
- Notifies SSE progress subscribers

**Fire-and-forget tasks:** If the `identifier` starts with `_noreply_`, result delivery is skipped. The task is still recorded but no ASGI/HTTP roundtrip occurs.

#### 3. Delegation (existing task_id + destination_agent_id set)

Hands off an active task to a different agent, preserving the same task_id.

- Validates ACL and handler permissions
- Increments `width_count` (capped by `MAX_WIDTH`)
- Updates `handler_agent_id`
- Injects `available_destinations` for the new handler
- Delivers to the new handler

### Access Control (ACL)

ACL is resolved in two tiers:

**1. Individual allowlist (highest priority):** If `individual_allowlist` has any entries for an agent, only those explicit destinations are permitted. Group rules are entirely bypassed.

**2. Group-based routing:** Agents belong to `inbound_groups` and `outbound_groups`. The `group_allowlist` table defines which outbound groups may reach which inbound groups.

**Default group routing rules:**

| From (outbound) | To (inbound) | Purpose |
|-----------------|--------------|---------|
| `core` | `infra`, `tool`, `usertool`, `channel` | Orchestrator can reach everything |
| `channel` | `core` | User messages route through orchestrator |
| `tool` | `infra` | Tools can call LLM |
| `usertool` | `infra`, `tool` | User-tools can call LLM and stateless tools |
| `notify` | `core`, `channel` | Proactive notifications (reminder/cron) |
| `bridge` | `tool`, `infra` | MCP bridge exposes stateless tools only |
| `admin` | `core`, `tool`, `usertool`, `infra`, `channel` | Admin has full access |

### Proxy Files

Files are transferred between agents through the router's proxy file system. A `ProxyFile` object has three protocols:

| Protocol | Description |
|----------|-------------|
| `router-proxy` | File stored in the router's vault, accessed via `/files/{task_id}/{filename}?key=...` |
| `http` | File served by an external agent via HTTP (router fetches and converts to `router-proxy`) |
| `localfile` | File on the shared filesystem (used by embedded agents, router copies to vault) |

**Lifecycle:**
1. Agent includes a `ProxyFile` in its routing payload
2. Router ingests the file (downloads HTTP, copies localfile) into `proxyfiles/{task_id}/`
3. Router issues a per-file access `key` and replaces the reference with a `router-proxy` ProxyFile
4. Destination agent fetches the file from `/files/{task_id}/{filename}?key=...`
5. Background GC deletes files whose tasks have reached terminal state (runs hourly)

**Size limits:** Files exceeding `MAX_FILE_BYTES` (default 50 MB) are rejected.

### Agent Types

#### Embedded Agents

- Run in-process with the router
- Loaded from subdirectories of `agents/`
- Communicate via zero-overhead ASGI transport (`httpx.ASGITransport`)
- Share the router's filesystem (can use `localfile` protocol)
- Auto-registered on startup with generated auth tokens
- Always considered "alive"

#### External Agents

- Run as separate processes
- Communicate over HTTP
- Register with the router using one-time invitation tokens
- Receive tasks at their `/receive` endpoint
- Health-checked periodically (configurable interval)
- Can run on different hosts or in containers

### Agent Discovery

Each agent publishes an `AgentInfo` describing its capabilities:

```json
{
    "agent_id": "web_agent",
    "description": "Web research agent. Searches the web and reads pages...",
    "input_schema": "llmdata: LLMData",
    "output_schema": "content: str",
    "required_input": ["llmdata"],
    "hidden": false,
    "documentation_url": "file:///app/agents/web_agent/AGENT.md"
}
```

The `input_schema` string is parsed into JSON Schema for LLM tool generation. Supported types: `str`, `int`, `bool`, `float`, `dict`, `ProxyFile`, `LLMData`, `AgentOutput`, `List[X]`, `Optional[X]`.

**Hidden agents** (e.g., `llm_agent`, `mcp_server`, `web_admin`) are excluded from LLM tool generation but remain routable via ACL.

**Agent documentation:** Agents can provide a `documentation_url` pointing to a markdown file. The router fetches it, stores it in the proxy vault, and makes it available to other agents via `fetch_agent_documentation` tool calls.

### Agent Health

A background loop probes external agents periodically:

- Sends `GET /health` to each agent's base URL
- Agents that respond with status < 500 are marked alive
- Dead agents are removed from the alive set (but not unregistered)
- Every N cycles (configurable), agent info is refreshed via `POST /refresh-info`
- Embedded agents are always alive

### Progress Events

Agents can push real-time progress events during task execution. Clients subscribe via SSE.

**Event types:** `thinking`, `tool_call`, `tool_result`, `status`, `chunk`, `done`

- `POST /tasks/{task_id}/progress` — Push an event (agent-authenticated)
- `GET /tasks/{task_id}/progress` — Subscribe to SSE stream (agent-authenticated)

The stream closes when the task completes, the client disconnects, or a 5-minute inactivity timeout is reached.

## Background Tasks

The router runs three background loops:

| Loop | Interval | Purpose |
|------|----------|---------|
| **Timeout sweep** | 60s | Marks active tasks past `timeout_at` as `timeout`, propagates error to origin |
| **Proxy file GC** | 3600s | Deletes files on disk for completed/failed/timed-out tasks |
| **Agent health** | Configurable (default 60s) | Probes external agents, updates alive set, periodically refreshes agent info |

## API Reference

### Agent Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/route` | Agent Bearer | Submit a routing payload (spawn, result, delegate) |
| `POST` | `/onboard` | Invitation token | Register a new external agent |
| `GET` | `/health` | None | Liveness probe |
| `GET` | `/files/{task_id}/{filename}` | File key (query param) | Download a proxy file |
| `GET` | `/docs/{agent_id}` | File key (query param) | Download agent documentation |
| `GET` | `/tasks/{task_id}/progress` | Agent Bearer | SSE stream of progress events |
| `POST` | `/tasks/{task_id}/progress` | Agent Bearer | Push a progress event |
| `PUT` | `/agent-info` | Agent Bearer | Update own agent metadata (partial merge) |
| `GET` | `/agent/destinations` | Agent Bearer | List ACL-filtered reachable agents |

### Admin Endpoints (require ADMIN_TOKEN)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/admin/invitation` | Create invitation token with group membership |
| `GET` | `/admin/agents` | List all registered agents (tokens excluded) |
| `DELETE` | `/admin/agents/{id}` | Remove an external agent |
| `PATCH` | `/admin/agents/{id}/groups` | Update agent group membership |
| `GET` | `/admin/agents/{id}/config` | Read embedded agent config.json |
| `PUT` | `/admin/agents/{id}/config` | Write embedded agent config.json |
| `GET` | `/admin/agents/{id}/config-example` | Read config field descriptions |
| `GET` | `/admin/agents/{id}/documentation` | Read agent documentation content |
| `PUT` | `/admin/agents/{id}/documentation` | Create/overwrite agent documentation |
| `POST` | `/admin/agents/{id}/refresh-info` | Trigger agent info refresh |
| `POST` | `/admin/group-allowlist` | Add a group routing rule |
| `POST` | `/admin/individual-allowlist` | Add an individual routing rule |
| `GET` | `/admin/tasks` | List tasks with filtering |

### Onboarding Flow

1. Admin creates an invitation via `POST /admin/invitation` with group membership
2. Agent calls `POST /onboard` with the invitation token, its endpoint URL, and `AgentInfo`
3. Router validates the token, generates an `auth_token` and `agent_id`, registers the agent
4. Router returns `agent_id`, `auth_token`, group membership, and `available_destinations`
5. Agent saves credentials to `data/credentials.json` for subsequent restarts

On restart, agents reconnect using saved credentials and call `PUT /agent-info` to refresh their metadata.

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `router.db` | SQLite database path |
| `PROXYFILE_DIR` | `proxyfiles` | Proxy file storage directory |
| `AGENTS_DIR` | `agents` | Embedded agents directory |
| `GLOBAL_TIMEOUT_HOURS` | `1` | Task timeout in hours |
| `MAX_DEPTH` | `10` | Maximum task nesting depth |
| `MAX_WIDTH` | `50` | Maximum delegations per task |
| `MAX_PAYLOAD_BYTES` | `1048576` (1 MB) | Maximum routing payload size |
| `MAX_FILE_BYTES` | `52428800` (50 MB) | Maximum proxy file size |
| `ADMIN_TOKEN` | (required) | Admin API bearer token |
| `EMBEDDED_AGENT_TIMEOUT` | `300` | ASGI timeout for embedded agent calls (seconds) |
| `AGENT_HEALTH_INTERVAL` | `60` | Health check interval (seconds) |
| `AGENT_HEALTH_INITIAL_DELAY` | `30` | Delay before first health check (seconds) |
| `AGENT_INFO_REFRESH_CYCLES` | `10` | Refresh agent info every N health cycles |

### Database Schema

The router uses SQLite in WAL mode with the following tables:

| Table | Purpose |
|-------|---------|
| `tasks` | Task lifecycle tracking |
| `events` | Audit log of all routing events (spawn, result, delegation, error) |
| `agents` | Agent registry (credentials, groups, info, documentation path) |
| `invitation_tokens` | One-time onboarding tokens |
| `group_allowlist` | Group-level ACL rules |
| `individual_allowlist` | Agent-level ACL overrides |
| `proxy_files` | File vault registry (key, path, filename, associated task) |
