# Backplaned

A lightweight, self-hosted multi-agent orchestration platform. A central router handles task routing, access control, and file transfer between pluggable agents over a unified HTTP protocol. Ships with a personal assistant suite: LLM gateway, long-term memory, web research, code execution, document conversion, knowledge base, reminders, scheduled tasks, and Telegram/Discord/MCP bridges.

## Architecture

![architecture](/docs/images/architecture.png)

All communication flows through the **router**, which acts as an ESB-style message broker. It manages task lifecycle (create, route, complete, timeout), proxy file storage, agent registration via invitation tokens, and group-based ACL.

**Embedded agents** run in-process with the router via zero-latency ASGI transport. **External agents** are separate processes communicating over HTTP — they can run on different hosts or in containers.

## Showcases

![showcase_coding](/docs/images/showcase_coding.gif) ![showcase_document](/docs/images/showcase_document.gif)

## Agents

| Agent | Type | Group | Description |
|-------|------|-------|-------------|
| **core_personal_agent** | Embedded | `core` | Main orchestrator. Maintains per-session chat history, runs LLM agent loop with parallel tool calling, manages long-term memory. |
| **llm_agent** | Embedded | `infra` | Centralized LLM inference gateway. Supports OpenAI, Anthropic, Google Generative AI, and OpenAI-compatible providers with fallback chains and per-user model ACL. |
| **md_converter** | Embedded | `tool` | Converts documents (PDF, DOCX, PPTX, XLSX, HTML, images, etc.) to Markdown. Optional LLM-based OCR for scanned documents. |
| **memory_agent** | Embedded | `usertool` | Long-term memory via LanceDB (local). LLM-powered fact extraction and consolidation. Per-user add/search operations. |
| **web_agent** | Embedded | `tool` | Web research agent. Searches via SearXNG or Brave and fetches pages with LLM-driven multi-step research loops. |
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
- A search backend for `web_agent` — either a SearXNG instance with JSON output enabled, or a Brave Search API key ([https://brave.com/search/api/](https://brave.com/search/api/)). The Docker deployment ships a preconfigured SearXNG container; for bare metal you must run your own (see [SearXNG docs](https://docs.searxng.org/)) and set `SEARXNG_BASE_URL` in `start.config`.

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
LLM_MODEL="gemma4:26b"
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

## Telegram User Registration

The `channel_agent` bridges Telegram to the router. Registration is invitation-based: an admin generates a one-time invitation token through the channel_agent web UI, and the Telegram user consumes it by sending a `/register` command to the bot.

### 1. Create a Telegram bot

1. In Telegram, open a chat with [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Follow the prompts to pick a name and username. BotFather will reply with an HTTP API token.
3. Set the token in `start.config`:
   ```bash
   TELEGRAM_TOKEN="123456:ABC-your-bot-token-from-BotFather"
   ```
4. Restart the stack (`./start.sh` for bare metal, or `docker compose restart` for Docker). On startup, channel_agent reads the token and begins polling Telegram for messages.

### 2. Log in to the channel_agent web UI

The channel_agent web UI is served on **port 8081** by default (set by `CHANNEL_PORT` in `start.config` for bare metal, or `docker/.env` for Docker).

1. Open `http://<host>:8081` in your browser.
2. Log in with the admin password from `ADMIN_PASSWORD` in `start.config`.
3. Change the password on first login via the account menu if desired.

### 3. Create an invitation token

1. Go to the **Users** tab.
2. Under **Invitation Tokens**, enter the `user_id` you want to assign to the new user (this is the internal identifier the core agent uses — one per person).
3. Click **Invite User**. A new token appears in the list with a default 24-hour TTL.
4. (Optional) Click **Config** next to the token to pre-configure the user's settings before they register. You can set:
   - **Model ID** — Which LLM model the user will use (from `llm_agent` config)
   - **Summarization model ID** — Model used for memory/history summarization
   - **Timezone** — IANA timezone (e.g. `America/Los_Angeles`, `Asia/Seoul`)
   - **System prompt** — Custom system prompt for the user's sessions
   - **Verbose mode** — If on, the bot streams LLM thinking and tool calls to the chat
   - **Core agent** — Which orchestrator agent this user routes through (defaults to `core_personal_agent`)
5. Copy the token string from the table and send it to the user over a secure channel.

### 4. User registers on Telegram

The user opens a chat with your bot on Telegram and sends:

```
/register <token>
```

For example:

```
/register aB3xY9_long-invitation-token-string
```

If the token is valid and unexpired, the bot replies with `Registered as <user_id>. Use /config to review your configuration.` Any pre-configured settings from step 3 are automatically applied.

Invitation tokens are **single-use** and expire after their TTL (default 24 hours). Invalid or expired tokens return `Invalid or expired invitation token.`

**Rate limiting:** Unregistered users are rate-limited (default: 5 attempts per hour, configurable via `RATE_LIMIT_WINDOW` and `RATE_LIMIT_MAX_TRIALS` in `start.config`) to prevent brute-forcing tokens.

### 5. Everyday use

After registering, the user simply sends messages and files to the bot. The channel_agent routes each message to `core_personal_agent`, which runs the full agent loop and replies.

Useful slash commands for registered users:

| Command | Description |
|---------|-------------|
| `/help` (or `/start`) | Show the list of commands |
| `/new` | Start a new chat session (archives current history) |
| `/stop` | Stop the currently running task |
| `/tokens` | Show estimated token usage for the current session |
| `/agents` | List available agents and their capabilities |
| `/config` | Show your current configuration |
| `/config <instruction>` | Modify your config in natural language (e.g. `/config set my timezone to Europe/Berlin`) |
| `/model` | List models you can use |
| `/model <id>` | Switch to a different model |
| `/link <agent_id>` | Talk directly to a specialized agent (skips orchestrator) |
| `/link` | List agents you can link to |
| `/unlink` | End the direct agent link |

Files (images, documents, etc.) can be uploaded directly — the bot forwards them to the orchestrator, which can pass them to tools like `md_converter`, `kb_agent`, or `coding_agent`.

### Managing registered users

Back in the channel_agent web UI (**Users** tab), admins can:

- View all registered users and their Telegram/Discord mappings
- Toggle verbose mode per user
- Override the core agent per user (useful for routing power users to a different orchestrator)
- Delete user mappings (revokes access; they'll need a new invitation)
- Revoke unused invitation tokens

### Discord

The Discord flow is identical: set `DISCORD_TOKEN` in `start.config`, invite the bot to a server, and have users send `/register <token>` in a DM with the bot. The same invitation tokens work for both platforms.

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
