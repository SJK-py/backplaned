# Backplaned

A lightweight, self-hosted multi-agent orchestration platform. A central router handles task routing, access control, and file transfer between pluggable agents over a unified HTTP protocol. Ships with a personal assistant suite: LLM gateway, long-term memory, web research, code execution, document conversion, knowledge base, reminders, scheduled tasks, and Telegram/Discord/MCP bridges.

## Showcases

![showcase_coding](/docs/images/showcase_coding.gif) ![showcase_document](/docs/images/showcase_document.gif)

## Architecture

![architecture](/docs/images/architecture.png)

All communication flows through the **router**, which acts as an ESB-style message broker. It manages task lifecycle (create, route, complete, timeout), proxy file storage, agent registration via invitation tokens, and group-based ACL.

**Embedded agents** run in-process with the router via zero-latency ASGI transport. **External agents** are separate processes communicating over HTTP — they can run on different hosts or in containers.

## Agents

| Agent | Type | Group | Description |
|-------|------|-------|-------------|
| **core_personal_agent** | Embedded | `core` | Main orchestrator. Maintains per-session chat history, runs LLM agent loop with parallel tool calling, manages long-term memory. |
| **llm_agent** | Embedded | `infra` | Centralized LLM inference gateway. Supports OpenAI, Anthropic, Google Generative AI, and OpenAI-compatible providers with fallback chains and per-user model ACL. |
| **md_converter** | Embedded | `tool` | Converts documents (PDF, DOCX, PPTX, XLSX, HTML, images, etc.) to Markdown. Optional LLM-based OCR for scanned documents. |
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

## Quick Start (Bare Metal)

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- An OpenAI-compatible LLM endpoint (local or cloud)

### Setup

```bash
git clone https://github.com/SJK-py/backplaned.git backplaned
cd backplaned

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
git clone https://github.com/SJK-py/backplaned.git backplaned
cd backplaned

# 1. Configure start.config (agent settings: LLM, embeddings, passwords)
cp start.config.example start.config
nano start.config   # Set ADMIN_TOKEN, ADMIN_PASSWORD, LLM_BASE_URL, LLM_MODEL
                    # Uncomment EXCLUDE_AGENTS="coding_agent"

# 2. Configure docker/.env (Docker-specific: ports, tokens)
cp docker/.env.example docker/.env
nano docker/.env     # Set ADMIN_TOKEN (must match start.config)

# 3. Launch
cd docker
docker compose up -d

# 4. Register coding agent (first time only):
#    a. Open web admin UI (http://localhost:8080)
#    b. Go to Invitations tab → create token with group "usertool"
#    c. Open coding agent UI (http://localhost:8100)
#    d. Go to Setup tab → paste the invitation token → Register
```

Two containers:
- **router** — Router + embedded agents + lightweight external agents. Reads `start.config` (mounted as volume) at startup to populate agent configurations.
- **coding** — Isolated coding agent sandbox. Requires one-time registration via web UI after first boot.

> **Important:** `ADMIN_TOKEN` in `docker/.env` must match the value in `start.config`. The router uses it for API authentication, and the web admin agent uses it to manage agents and create invitation tokens.

### How Docker config works

| File | Purpose | Read by |
|---|---|---|
| `start.config` | Agent settings (LLM endpoints, embeddings, Telegram tokens, etc.) | `start.sh` inside the router container (mounted as volume) |
| `docker/.env` | Docker-specific (ports, ADMIN_TOKEN, DATA_ROOT) | `docker-compose.yml` for variable substitution |
| `data/*/config.json` | Runtime agent settings (editable via web admin UI) | Each agent at runtime (persisted on volumes) |
| `data/*/.env` | Agent secrets (auto-generated by start.sh) | Each agent at startup (persisted on volumes) |

On first startup, `start.sh` creates `data/config.json` and `data/.env` for each agent from templates and `start.config` values. On subsequent starts, existing configs are preserved — only secrets (passwords, tokens) are re-propagated.

## Documentation

Detailed documentation is available in the [`docs/`](docs/) directory:

- **[Router System Overview](docs/router.md)** — Architecture, task lifecycle, ACL, proxy files, and all API endpoints
- **[Agent Reference](docs/agents.md)** — Features, configuration, and environment variables for every included agent
- **[Agent Development Guide](docs/agent-development.md)** — How to build your own embedded or external agent
- **[Contributing Guide](docs/contributing.md)** — Project setup, conventions, and contribution workflow

## Project Structure

```
backplaned/
├── router.py                 # Central router
├── helper.py                 # Shared client library (models, builders, RouterClient)
├── config_ui.py              # Shared config UI utilities
├── start.sh                  # Bare-metal launcher
├── start.config.example      # User configuration template
├── .env.example              # Router environment defaults
├── pyproject.toml            # Dependencies
│
├── backplaned/               # Minimal package marker (for editable installs)
│   └── __init__.py
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
├── docs/                     # Documentation
│   ├── router.md
│   ├── agents.md
│   ├── agent-development.md
│   └── contributing.md
│
└── docker/
    ├── docker-compose.yml
    ├── .env.example
    ├── Dockerfile.router
    └── Dockerfile.coding
```

## License

MIT
