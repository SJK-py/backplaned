# Agora

A lightweight, self-hosted multi-agent orchestration platform. A central router handles task routing, access control, and file transfer between pluggable agents over a unified HTTP protocol. Ships with a personal assistant suite: LLM gateway, long-term memory, web research, code execution, document conversion, knowledge base, reminders, scheduled tasks, and Telegram/Discord/MCP bridges.

## Architecture

```
  Telegram / Discord           Web Admin           Claude Desktop, Cursor, ...
         |                         |                          |
    channel_agent                  |                     mcp_server
         \                         |                (Router-as-MCP Bridge)
          \                        |                        /
       ┌───────────────────────────────────────────────────────┐
       │                            Router                     │
       │              task routing · ACL · proxy files         │
       └──┬───────────┬──────────────┬─────────────────────┬───┘
          │           │              │                     │
   core_personal  ┌───┴───┐  ┌───────┴────────┐  ┌─────────┴────────┐
      _agent      │ infra │  │      tool      │  │    usertool      │
  (Orchestrator,  ├───────┤  ├────────────────┤  ├──────────────────┤
      Session     │  llm  │  │  md_converter  │  │   memory_agent   │
    Management)   │ agent │  │    web_agent   │  │   coding_agent   │
                  └───────┘  │    mcp_agent   │  │     kb_agent     │
                             │ (External MCP  │  │  reminder_agent  │
                             │ Server Bridge) │  │    cron_agent    │
                             └────────────────┘  └──────────────────┘
```

All communication flows through the **router**, which acts as an ESB-style message broker. It manages task lifecycle (create, route, complete, timeout), proxy file storage, agent registration via invitation tokens, and group-based ACL.

**Embedded agents** run in-process with the router via zero-latency ASGI transport. **External agents** are separate processes communicating over HTTP — they can run on different hosts or in containers.

## Agents

| Agent | Type | Group | Description |
|-------|------|-------|-------------|
| **core_personal_agent** | Embedded | `core` | Main orchestrator. Maintains per-session chat history, runs LLM agent loop with parallel tool calling, manages long-term memory. |
| **llm_agent** | Embedded | `infra` | Centralized LLM inference gateway. Supports OpenAI, Anthropic, and OpenAI-compatible providers with fallback chains and per-user model ACL. |
| **md_converter** | Embedded | `tool` | Converts documents (PDF, DOCX, PPTX, XLSX, HTML, etc.) to Markdown. Optional LLM-based OCR for scanned documents. |
| **memory_agent** | Embedded | `usertool` | Long-term memory via LanceDB (local). LLM-powered fact extraction and consolidation. Per-user add/search operations. |
| **web_agent** | Embedded | `tool` | Web research agent. Searches (DuckDuckGo, SearXNG, Brave) and fetches pages with LLM-driven multi-step research loops. |
| **channel_agent** | External | `channel` | Bridges Telegram and Discord to the router. Handles slash commands, file uploads, inline progress streaming. Also delivers outbound messages to users when called by other agents. |
| **coding_agent** | External | `usertool` | Sandboxed code execution workspace. Writes, runs, and iterates on code with file I/O. Per-user security policies. |
| **reminder_agent** | External | `usertool`+`notify` | Calendar events, tasks, and reminders with natural language. Proactive notifications via checker loop. |
| **cron_agent** | External | `usertool`+`notify` | Scheduled recurring tasks with cron expressions. Autonomous execution with result reporting. |
| **kb_agent** | External | `usertool` | Knowledge base with hybrid vector + full-text search. Stores Markdown documents in per-user LanceDB databases. |
| **mcp_agent** | External | `tool` | Outbound MCP gateway. Connects to external MCP tool servers (stdio/SSE) and exposes their tools to the router. |
| **mcp_server** | External | `bridge` | Inbound MCP bridge. Exposes router agents as MCP tools to external clients (Claude Desktop, Cursor, etc.). |
| **web_admin** | External | `admin` | Admin web frontend. Task management, agent configuration, direct messaging, and system monitoring. |

## Access Control

Agents are organized into groups with directional routing rules:

| From (outbound) | To (inbound) | Purpose |
|-----------------|--------------|---------|
| `core` | `infra`, `tool`, `usertool`, `channel` | Orchestrator can reach everything |
| `channel` | `core` | User messages route through orchestrator |
| `tool` | `infra` | Tools can call LLM |
| `usertool` | `infra`, `tool` | User-tools can call LLM and stateless tools |
| `notify` | `core`, `channel` | Proactive notifications (reminder/cron) |
| `bridge` | `tool`, `infra` | MCP bridge exposes stateless tools only |
| `admin` | `core`, `tool`, `usertool`, `infra`, `channel` | Admin has full access |

## Quick Start (Bare Metal)

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- An OpenAI-compatible LLM endpoint (local or cloud)

### Setup

```bash
git clone https://github.com/SJK-py/agora.git agora
cd agora

# Create virtual environment and install dependencies
uv venv
uv pip install -e .

# Configure — edit start.config with your LLM endpoint and passwords
cp start.config.example start.config
nano start.config  # Set ADMIN_TOKEN, ADMIN_PASSWORD, LLM_BASE_URL, LLM_MODEL at minimum

# Launch all services
./start.sh
```

The router starts on port 8000. The web admin UI is at port 8080.

### Minimal start.config

```bash
ADMIN_TOKEN="your-secret-admin-token"
ADMIN_PASSWORD="your-admin-password"
LLM_PROVIDER="openai_compat"
LLM_BASE_URL="http://localhost:11434/v1"   # e.g. Ollama
LLM_MODEL="llama3.1"
```

For full configuration options (memory, embeddings, OCR, Telegram, Discord, ports), see the comments in `start.config.example`.

## Quick Start (Docker)

```bash
git clone https://github.com/SJK-py/agora.git agora
cd agora

# Configure
cp start.config.example start.config
nano start.config

# Launch
cd docker
docker compose up -d
```

Two containers:
- **router** — Router + embedded agents + lightweight external agents
- **coding** — Isolated coding agent sandbox

## Configuration

### start.config

Central configuration file sourced by `start.sh`. Values are propagated into each agent's `.env` and `config.json` at startup. See `start.config.example` for all options with descriptions.

### Agent config.json (Embedded Agents)

Each embedded agent under `agents/` has:
- `config.json` — Active configuration (auto-populated from `start.config` on startup)
- `config.example` — Schema documentation (shown in the web admin UI config tab)

Key configurations:
- **llm_agent** — Model definitions, provider settings, retry policy, per-user model ACL
- **memory_agent** — LLM model ID (routed via llm_agent), embedding endpoint, LanceDB table settings
- **md_converter** — OCR toggle and VLM endpoint for scanned document support
- **web_agent** — Search provider (DuckDuckGo/SearXNG/Brave), fetch limits

### Agent .env (External Agents)

Each external agent under `agents_external/` has:
- `.env` — Active environment (auto-populated from `start.config` on startup)
- `.env.example` — Template with all supported variables

## Key Concepts

### Tasks and Routing

Agents communicate by posting **routing payloads** to the router. A payload can:
- **Spawn** a new task (`task_id: "new"`) targeting a destination agent
- **Report** a result (`destination_agent_id: null`) back to the origin agent
- **Delegate** an existing task to a different handler

The router enforces ACL rules, manages task lifecycle (timeouts, depth/width limits), and handles file transfer.

### Proxy Files

Files are transferred between agents via the router's proxy file system. Agents upload files which the router stores in a vault and issues access keys. Files are referenced as `ProxyFile` objects with protocol (`router-proxy`, `http`, `localfile`) and are automatically garbage-collected when their associated tasks complete.

### Embedded vs External Agents

**Embedded agents** are Python modules loaded in-process by the router. They communicate via zero-overhead ASGI transport and share the router's filesystem (enabling `localfile` protocol). Add a new embedded agent by creating a directory under `agents/` with an `agent.py` that exposes a FastAPI `app` and an `AGENT_INFO` constant.

**External agents** run as separate processes and communicate over HTTP. They register with the router using invitation tokens and receive tasks at their `/receive` endpoint. Add a new external agent by creating a directory under `agents_external/`, implementing the `/receive` and `/refresh-info` endpoints, and adding a launch block to `start.sh`.

### Agent Onboarding

External agents register with the router using single-use invitation tokens:

1. `start.sh` creates an invitation via `POST /admin/invitation` with the agent's group membership
2. The agent calls `POST /onboard` with the token and receives an `auth_token` + `agent_id`
3. Credentials are saved to `data/credentials.json` for subsequent restarts

## Project Structure

```
agora/
├── router.py                 # Central router
├── helper.py                 # Shared client library (models, builders, RouterClient)
├── start.sh                  # Bare-metal launcher
├── start.config              # User configuration
├── pyproject.toml            # Dependencies
│
├── agents/                   # Embedded agents
│   ├── core_personal_agent/
│   ├── llm_agent/
│   ├── md_converter/
│   ├── memory_agent/
│   └── web_agent/
│
├── agents_external/          # External agents
│   ├── channel_agent/
│   ├── coding_agent/
│   ├── cron_agent/
│   ├── kb_agent/
│   ├── mcp_agent/
│   ├── mcp_server/
│   ├── reminder_agent/
│   └── web_admin/
│
└── docker/
    ├── docker-compose.yml
    ├── Dockerfile.router
    └── Dockerfile.coding
```

## API Overview

### Router Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/route` | Agent Bearer | Submit a routing payload (spawn, result, delegate) |
| `POST` | `/onboard` | Invitation token | Register a new external agent |
| `GET` | `/health` | None | Health check |
| `GET` | `/files/{task_id}/{filename}` | File key | Download a proxy file |
| `GET` | `/tasks/{task_id}/progress` | Agent Bearer | SSE stream of task progress events |
| `PUT` | `/agent-info` | Agent Bearer | Update agent metadata |
| `GET` | `/agent/destinations` | Agent Bearer | List ACL-filtered reachable agents |

### Admin Endpoints (require ADMIN_TOKEN)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/admin/invitation` | Create invitation token with group membership |
| `GET` | `/admin/agents` | List all registered agents |
| `PUT` | `/admin/agents/{id}/groups` | Update agent group membership |
| `POST` | `/admin/group-allowlist` | Add a group routing rule |
| `GET` | `/admin/tasks` | List tasks with filtering |
| `POST` | `/admin/agents/{id}/refresh-info` | Trigger agent info refresh |

## License

MIT
