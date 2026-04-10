# Contributing Guide

Thank you for your interest in contributing to Backplaned. This guide covers project setup, conventions, and the contribution workflow.

## Development Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended package manager)
- Git
- An OpenAI-compatible LLM endpoint for testing (e.g., [Ollama](https://ollama.com/))

### Getting Started

```bash
git clone https://github.com/SJK-py/backplaned.git
cd backplaned

# Create virtual environment and install dependencies
uv venv
uv pip install -e .

# Configure
cp start.config.example start.config
# Edit start.config with your LLM endpoint and admin credentials

# Run
./start.sh
```

### Running Individual Agents

For development, you can run specific agents in isolation:

```bash
# Router only
.venv/bin/uvicorn router:app --host 0.0.0.0 --port 8000

# A specific external agent (after router is running)
cd agents_external/reminder_agent
python agent.py
```

External agents need their `data/.env` configured with `ROUTER_URL`, `AGENT_PORT`, and either `INVITATION_TOKEN` (first run) or saved credentials in `data/credentials.json`.

## Project Structure

```
backplaned/
├── router.py           # Central router (all routing logic, ACL, proxy files)
├── helper.py           # Shared library (models, RouterClient, builders)
├── config_ui.py        # Shared config UI utilities for agent web UIs
├── start.sh            # Startup orchestrator
├── agents/             # Embedded agents (loaded by router at startup)
├── agents_external/    # External agents (separate processes)
├── docker/             # Docker deployment files
└── docs/               # Documentation
```

### Key Files

| File | Responsibility |
|------|----------------|
| `router.py` | All routing logic, task lifecycle, ACL, proxy files, agent registration, API endpoints |
| `helper.py` | Pydantic models (`ProxyFile`, `AgentInfo`, `LLMData`, etc.), `RouterClient`, message builders, LLM tool builders, `ProxyFileManager` |
| `config_ui.py` | Reusable config editor endpoints for agent web UIs |
| `start.sh` | Config propagation from `start.config` into agent `.env`/`config.json`, agent bootstrapping and startup sequencing |

### Agent Structure

Each agent (embedded or external) follows a consistent pattern:

| File | Purpose |
|------|---------|
| `agent.py` | Main application — FastAPI app, AGENT_INFO, `/receive` endpoint, task processing |
| `config.default.json` | Default runtime config values (source-controlled) |
| `config.example` | JSON describing each config field (shown in web admin UI) |
| `.env.example` | Environment variable template (external agents only) |
| `data/` | Runtime data directory (gitignored) |

## Conventions

### Code Style

- Python 3.12+ features are welcome (type hints, match statements, etc.)
- Use type hints for function signatures
- Follow existing code patterns — consistency across agents is valued
- Prefer `asyncio` and `httpx` for async operations
- Use `logging` module for log output, not `print()` (exception: router startup messages)

### Naming

- Agent directories use `snake_case` (e.g., `my_agent`)
- Agent IDs match their directory name
- Config keys use `UPPER_SNAKE_CASE`
- Python modules and variables use `snake_case`

### Configuration

- Infrastructure config (URLs, ports, credentials) goes in `.env`
- Behavioral config (limits, toggles, model IDs) goes in `config.json`
- All config keys should have sensible defaults
- Document every config field in `config.example`

### Error Handling

- Return `status_code >= 400` in agent results to indicate failure
- Use descriptive error messages in `AgentOutput.content`
- Use timeouts for all network calls and sub-agent interactions
- Cap iteration counts to prevent runaway loops
- Never silently swallow errors in task processing — always report back

### Security

- Never log or expose auth tokens in responses
- Use `secrets.compare_digest()` for token comparison (constant-time)
- Validate and sanitize file paths — the router has `_is_safe_path()` and `_sanitize_task_id()` helpers
- External agents should verify the `Authorization` header on `/receive`
- Use `PasswordFile` from `helper.py` for admin password storage (PBKDF2-SHA256)

## Contribution Workflow

### 1. Create a Branch

```bash
git checkout -b feature/my-feature
# or
git checkout -b fix/bug-description
```

### 2. Make Changes

- Follow existing code patterns and conventions
- Add config defaults and examples for new config keys
- Update documentation if you're adding features or changing behavior

### 3. Test Locally

```bash
# Start the full stack
./start.sh

# Or test specific components
.venv/bin/python -c "from helper import AgentInfo; print('imports ok')"
```

Test your changes end-to-end:
- Start the router and relevant agents
- Verify agent registration (check web admin UI)
- Send test messages through the system
- Check that tasks complete successfully

### 4. Submit a Pull Request

- Write a clear PR title and description
- Reference any related issues
- Describe what you changed and why

## Adding a New Agent

See the [Agent Development Guide](agent-development.md) for a complete walkthrough. In summary:

### Embedded Agent Checklist

1. Create `agents/my_agent/agent.py` with `app` and `AGENT_INFO`
2. Create `agents/my_agent/config.default.json` with sensible defaults
3. Create `agents/my_agent/config.example` with field descriptions
4. Add group assignment in `router.py` `_EMBEDDED_AGENT_GROUPS`
5. Add the data directory to `docker/Dockerfile.router` if using Docker

### External Agent Checklist

1. Create `agents_external/my_agent/` with `agent.py`, `config.py`, configs
2. Implement `/receive`, `/health`, and `/refresh-info` endpoints
3. Add `.env.example` with required environment variables
4. Add bootstrap and startup logic to `start.sh`
5. Add the agent to `docker/Dockerfile.router` COPY commands
6. Add the data directory and port mapping to `docker/docker-compose.yml`
7. Update `start.config.example` if the agent introduces new user-facing config

## Docker Development

### Building

```bash
cd docker
docker compose build
```

### Rebuilding a Single Container

```bash
docker compose build router   # or: coding
docker compose up -d router
```

### Viewing Logs

```bash
docker compose logs -f router
docker compose logs -f coding
```

### Volume Permissions

If you get permission errors, ensure your host UID/GID match the container user:

```bash
docker compose build --build-arg UID=$(id -u) --build-arg GID=$(id -g)
```

## Architecture Decisions

### Why SQLite?

SQLite in WAL mode provides sufficient performance for the router's workload (task tracking, agent registry, proxy file metadata). It avoids external dependencies and simplifies deployment. The codebase is structured for a straightforward migration to `aiosqlite` if needed for high-traffic deployments.

### Why In-Process Embedded Agents?

Embedded agents benefit from zero-latency ASGI transport and shared filesystem access. This eliminates HTTP overhead for tightly-coupled agents (LLM inference, memory, document conversion) while keeping the same interface contract as external agents.

### Why Invitation-Based Onboarding?

Invitation tokens provide a controlled registration mechanism. Admins create tokens with specific group memberships, and agents use them once to register. This prevents unauthorized agents from joining the system while allowing self-service setup for known agents.

### Why Group-Based ACL?

Group-based routing rules provide a scalable way to define access policies. Instead of managing N*N individual agent permissions, you define policies between functional groups (core, infra, tool, usertool, channel, notify, bridge, admin). Individual overrides are available when needed.
