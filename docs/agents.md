# Agent Reference

This document covers every included agent's features, configuration, and environment variables.

## Configuration Layers

Each agent has up to three configuration sources:

| Layer | File | Purpose | Editable at runtime |
|-------|------|---------|---------------------|
| **Environment** | `data/.env` | Infrastructure: router URL, ports, credentials, secrets | No (requires restart) |
| **Runtime config** | `data/config.json` | Behavior: models, limits, feature toggles | Yes (via web admin UI) |
| **Defaults** | `config.default.json` | Template for initial `config.json` creation | No (source-controlled) |

`start.sh` propagates values from `start.config` into each agent's `.env` and `config.json` on first startup. On subsequent starts, existing configs are preserved — only secrets are re-propagated.

---

## Embedded Agents

Embedded agents run in-process with the router. They are loaded from subdirectories of `agents/` and communicate via zero-latency ASGI transport.

### core_personal_agent

**Group:** `core`

Main orchestrator and personal assistant. This is the primary entry point for user conversations from channel_agent, web_admin, and mcp_server.

**Features:**
- Maintains per-session chat history with configurable token limits
- Runs a multi-turn LLM agent loop with parallel tool calling
- Automatically queries and updates long-term memory via memory_agent
- Delegates tasks to specialized agents based on user requests
- Supports file attachments (fetched via ProxyFileManager)
- Linked conversation history: carries forward context from previous sessions

**Input/Output:**
- Input: `user_id: str, session_id: str, message: str, files: Optional[List[ProxyFile]]`
- Output: `content: str, files: Optional[List[ProxyFile]]`

**Configuration (`config.json`):**

| Key | Default | Description |
|-----|---------|-------------|
| `CORE_LLM_AGENT_ID` | `llm_agent` | Agent ID for LLM inference |
| `CORE_LLM_MODEL_ID` | `""` | Model config key (empty = "default") |
| `CORE_MEMORY_AGENT_ID` | `memory_agent` | Agent ID for long-term memory |
| `CORE_HISTORY_TOKEN_LIMIT` | `32768` | Max tokens in conversation history |
| `CORE_MAX_AGENT_ITERATIONS` | `25` | Max LLM agent loop iterations |
| `CORE_AGENT_TIMEOUT` | `290` | Total timeout for agent loop (seconds) |
| `CORE_TOOL_TIMEOUT` | `240` | Timeout per tool call (seconds) |
| `CORE_LINK_HISTORY_TOKEN_RATIO` | `0.5` | Fraction of history budget for linked sessions |
| `CORE_LINK_TRUNCATION_KEEP_RATIO` | `0.5` | Keep ratio when truncating linked history |

---

### llm_agent

**Group:** `infra` | **Hidden:** Yes

Centralized LLM inference gateway. All LLM-using agents route inference through this agent, enabling centralized model management, per-user ACL, and provider abstraction.

**Features:**
- Supports multiple model configurations with named keys (e.g., "default", "fast", "vision")
- Providers: OpenAI, Anthropic, Google Generative AI, and any OpenAI-compatible endpoint (vLLM, llama.cpp, Ollama, etc.)
- Fallback chains: if a model fails, automatically retries with a configured fallback model
- Per-user model ACL: restrict which users can access which models
- Configurable retry policy with exponential backoff
- Accepts `LLMCall` payloads with full messages array, tool definitions, and per-call overrides

**Input/Output:**
- Input: `llmcall: LLMCall, model_id: Optional[str], user_id: Optional[str]`
- Output: `content: str` (JSON containing `content` and `tool_calls`)

**Configuration (`config.json`):**

| Key | Default | Description |
|-----|---------|-------------|
| `models` | (see below) | Named model configurations |
| `allowed_models` | `{}` | Per-user model ACL (`user_id → [model_ids]`) |
| `retry_count` | `2` | Retries per model before fallback |
| `retry_interval` | `5` | Initial retry delay (seconds) |
| `retry_interval_multiplier` | `2` | Retry backoff multiplier |
| `total_retry_count` | `6` | Max total retries across fallback chain |

**Model configuration:**

```json
{
    "models": {
        "default": {
            "provider": "openai_compat",
            "base_url": "http://localhost:11434/v1",
            "api_key": "",
            "model": "llama3.1",
            "max_tokens": 16384,
            "temperature": 1,
            "timeout": 300,
            "fallback": null,
            "available_to_all": true
        }
    }
}
```

| Model field | Description |
|-------------|-------------|
| `provider` | `openai_compat`, `openai`, `anthropic`, or `google` |
| `base_url` | API endpoint URL |
| `api_key` | API key (empty for local models without auth) |
| `model` | Model name/ID |
| `max_tokens` | Maximum output tokens per completion |
| `temperature` | Sampling temperature |
| `timeout` | Request timeout (seconds) |
| `fallback` | Model key to try if this one fails (e.g., `"fallback_model"`) |
| `available_to_all` | If false, only users listed in `allowed_models` can use this model |

---

### md_converter

**Group:** `tool`

Document-to-Markdown converter. Accepts files in various formats and returns Markdown text.

**Features:**
- Supported formats: PDF, DOCX, PPTX, XLSX, HTML, images (PNG, JPG, etc.), and more via [markitdown](https://github.com/microsoft/markitdown)
- Optional LLM-based OCR for scanned documents and images (uses a vision-capable model)
- Can return result as text content or as a `.md` file attachment
- Configurable content length limits with preview mode for large documents

**Input/Output:**
- Input: `file: ProxyFile, output_file: Optional[bool]`
- Output: `content: str, files: Optional[List[ProxyFile]]`

**Configuration (`config.json`):**

| Key | Default | Description |
|-----|---------|-------------|
| `OCR_ENABLED` | `false` | Enable LLM-based OCR |
| `OCR_BASE_URL` | `""` | VLM endpoint for OCR (falls back to LLM gateway) |
| `OCR_API_KEY` | `""` | API key for OCR endpoint |
| `OCR_MODEL` | `""` | Model name for OCR |
| `OCR_PROMPT` | `""` | Custom OCR prompt |
| `OCR_NO_PROMPT` | `true` | If true, use markitdown-ocr defaults |
| `MAX_CONTENT_LENGTH` | `30000` | Max content chars before truncation |
| `PREVIEW_LENGTH` | `2000` | Preview length for large documents |
| `OUTPUT_DIR` | `""` | Directory for output .md files |

---

### memory_agent

**Group:** `usertool`

Long-term memory store using LanceDB for vector search. Stores and retrieves per-user facts extracted by an LLM.

**Features:**
- Two operations: `add` (store content) and `search` (retrieve relevant memories)
- LLM-powered fact extraction: raw content is processed by an LLM to extract structured facts
- Fact consolidation: merges new facts with existing ones to avoid duplicates
- Vector similarity search via embedding model
- Per-user isolated tables in LanceDB
- Time-aware: stores timestamps and can provide timezone context

**Input/Output:**
- Input: `operation: str, content: str, user_id: str, count: Optional[int], timezone: Optional[str]`
- Output: `content: str` (JSON array for search results)

**Configuration (`config.json`):**

| Key | Default | Description |
|-----|---------|-------------|
| `LLM_AGENT_ID` | `llm_agent` | Agent for fact extraction |
| `LLM_MODEL_ID` | `""` | Model config key |
| `USER_MODEL_IDS` | `{}` | Per-user model overrides |
| `EMBED_BASE_URL` | `""` | Embedding API endpoint (required) |
| `EMBED_API_KEY` | `""` | Embedding API key |
| `EMBED_MODEL` | `""` | Embedding model name (required) |
| `EMBEDDING_DIMS` | `768` | Embedding vector dimensions |
| `DEFAULT_SEARCH_COUNT` | `5` | Default number of search results |

---

### web_agent

**Group:** `tool`

Web research agent that performs multi-step searches and page reads to produce sourced reports.

**Features:**
- Search providers: **SearXNG** (default) or **Brave Search API**. A SearXNG container is included in the Docker deployment.
- Multi-step research loop: search, read pages, refine query, search again
- LLM-driven: uses an LLM to decide which pages to fetch and how to synthesize results
- Configurable limits on searches, fetches, and iterations
- Time-aware (UTC)

**Input/Output:**
- Input: `llmdata: LLMData`
- Output: `content: str`

**Backend setup:**

- **Docker (bundled SearXNG):** The `searxng` service in `docker-compose.yml` is gated behind the `searxng` Compose profile. `docker/.env.example` ships with `COMPOSE_PROFILES=searxng` so the bundled container runs by default; `start.config.example` points `SEARXNG_BASE_URL` at `http://searxng:8080` over the Compose network. The settings file is inlined via `configs.content` (JSON output enabled, rate limiter disabled). Host-side port defaults to `8880` (`SEARXNG_PORT` in `docker/.env`).
- **Docker (your own SearXNG):** Clear `COMPOSE_PROFILES` in `docker/.env` so the bundled container doesn't start, then set `SEARXNG_BASE_URL` in `start.config` to your instance URL. Your SearXNG must have `search.formats: [html, json]` in its `settings.yml` — `web_agent` uses the JSON API.
- **Bare metal:** Run your own SearXNG (same JSON requirement) and set `SEARXNG_BASE_URL` accordingly. Alternatively, set `SEARCH_PROVIDER="brave"` and supply `BRAVE_API_KEY`.

**Configuration (`config.json`):**

| Key | Default | Description |
|-----|---------|-------------|
| `LLM_AGENT_ID` | `llm_agent` | Agent for LLM inference |
| `LLM_MODEL_ID` | `""` | Model config key |
| `USER_MODEL_IDS` | `{}` | Per-user model overrides |
| `SEARCH_PROVIDER` | `searxng` | Search provider (`searxng` or `brave`) |
| `SEARXNG_BASE_URL` | `""` | SearXNG instance URL (populated from `start.config`) |
| `BRAVE_API_KEY` | `""` | Brave Search API key |
| `SEARCH_MAX_RESULTS` | `5` | Max results per search query |
| `CONTENT_LEN_LIMIT` | `500` | Search result snippet length |
| `FETCH_MAX_CHARS` | `12000` | Max characters per fetched page |
| `FETCH_TIMEOUT` | `15` | Page fetch timeout (seconds) |
| `AGENT_TIMEOUT` | `120` | Total agent timeout (seconds) |
| `MAX_ITERATIONS` | `10` | Max agent loop iterations |
| `MAX_TOOL_CALLS` | `15` | Max total tool calls |
| `MAX_SEARCHES` | `2` | Max search operations |
| `MAX_FETCHES` | `3` | Max page fetch operations |

---

## External Agents

External agents run as separate processes and communicate with the router over HTTP. They register using invitation tokens and receive tasks at their `/receive` endpoint.

### channel_agent

**Group:** `channel`

Bridges Telegram and Discord messaging platforms to the router.

**Features:**
- Telegram bot integration (via python-telegram-bot)
- Discord bot integration (via raw WebSocket gateway)
- User registration with invitation tokens (`/register <token>`)
- Slash commands for settings (timezone, model, etc.)
- File upload support (images, documents)
- Inline progress streaming (shows LLM thinking/tool activity in real-time)
- Rate limiting for unregistered users
- Outbound message delivery: other agents can send direct messages to users

**Input/Output:**
- Input: `user_id: str, session_id: str, message: str`
- Output: `content: str`

**Configuration (`config.json`):**

| Key | Default | Description |
|-----|---------|-------------|
| `CORE_AGENT_ID` | `core_personal_agent` | Agent to route user messages to |
| `RATE_LIMIT_WINDOW` | `3600` | Rate limit window (seconds) |
| `RATE_LIMIT_MAX_TRIALS` | `5` | Max attempts from unregistered users |

**Environment variables (`.env`):**

| Variable | Description |
|----------|-------------|
| `TELEGRAM_TOKEN` | Telegram bot token from @BotFather |
| `DISCORD_TOKEN` | Discord bot token |
| `ROUTER_URL` | Router base URL |
| `AGENT_PORT` | Listen port (default 8081) |

---

### coding_agent

**Group:** `usertool`

Sandboxed code execution workspace. Each user gets an isolated workspace with configurable security policies.

**Features:**
- Per-user isolated workspaces with file persistence
- LLM-driven code writing, modification, and execution
- File I/O: read, write, and manage workspace files
- Attach files to results for downstream agents
- Configurable security policies per user: command blocking, path restrictions, network access
- Admin web UI for user management and security configuration
- Agent documentation support (SPEC.md)

**Input/Output:**
- Input: `llmdata: LLMData, user_id: str, files: Optional[List[ProxyFile]], timezone: Optional[str]`
- Output: `content: str, files: Optional[List[ProxyFile]]`

**Configuration (`config.json`):**

| Key | Default | Description |
|-----|---------|-------------|
| `LLM_AGENT_ID` | `llm_agent` | Agent for LLM inference |
| `DEFAULT_MODEL_ID` | `""` | Default model config key |
| `LLM_TIMEOUT` | `120` | LLM call timeout (seconds) |
| `TOOL_TIMEOUT` | `60` | Code execution timeout (seconds) |
| `WORKSPACE_ROOT` | `data/workspaces` | Root directory for user workspaces |
| `USER_CONFIG_PATH` | `data/user_config.json` | User security policies file |

**Docker deployment:** The coding agent runs in its own container with resource limits (default: 2 CPU cores, 4 GB RAM) and security restrictions (`no-new-privileges`, dropped `NET_RAW`).

---

### reminder_agent

**Group:** `usertool` + `notify`

Calendar events, tasks, and reminders managed via natural language.

**Features:**
- Natural language event/reminder creation, modification, and deletion
- LLM-driven scheduling with tool calling
- Proactive notification loop: periodically checks for due items and sends notifications
- Per-user timezone support
- Recurring events via RRULE (RFC 5545)
- Notifications delivered via core_personal_agent and channel_agent

**Input/Output:**
- Input: `llmdata: LLMData, user_id: str, session_id: str, timezone: Optional[str]`
- Output: `content: str`

**Configuration (`config.json`):**

| Key | Default | Description |
|-----|---------|-------------|
| `LLM_AGENT_ID` | `llm_agent` | Agent for LLM inference |
| `DEFAULT_MODEL_ID` | `""` | Default model config key |
| `CORE_AGENT_ID` | `core_personal_agent` | Agent for notification delivery |
| `LLM_TIMEOUT` | `120` | LLM call timeout (seconds) |
| `TOOL_TIMEOUT` | `60` | Tool execution timeout (seconds) |
| `CHECK_INTERVAL` | `30` | Notification check interval (seconds) |
| `CHECK_LOOKAHEAD_HOURS` | `72` | How far ahead to check for due items |

---

### cron_agent

**Group:** `usertool` + `notify`

Scheduled recurring tasks with cron expressions. Jobs run autonomously without user interaction.

**Features:**
- Natural language cron job creation, modification, and deletion
- Standard cron expression scheduling
- Autonomous execution: jobs trigger independently on schedule
- Result reporting via core_personal_agent and channel_agent
- Job history and logs
- LLM-driven management via tool calling

**Input/Output:**
- Input: `llmdata: LLMData, user_id: str, session_id: str, timezone: Optional[str]`
- Output: `content: str`

**Configuration (`config.json`):**

| Key | Default | Description |
|-----|---------|-------------|
| `LLM_AGENT_ID` | `llm_agent` | Agent for LLM inference |
| `DEFAULT_MODEL_ID` | `""` | Default model config key |
| `CORE_AGENT_ID` | `core_personal_agent` | Agent for job execution and notifications |
| `CHECK_INTERVAL` | `30` | Job schedule check interval (seconds) |
| `TOOL_TIMEOUT` | `120` | Tool execution timeout (seconds) |

---

### kb_agent

**Group:** `usertool`

Knowledge base with hybrid vector + full-text search over Markdown documents.

**Features:**
- Per-user isolated LanceDB databases
- Hybrid search: combines vector similarity (embeddings) with full-text search
- Document ingestion: accepts files (converted to Markdown via md_converter)
- Chunking with configurable overlap for large documents
- LLM-driven management via tool calling
- CRUD operations: store, search, list, delete documents

**Input/Output:**
- Input: `llmdata: LLMData, user_id: str, files: Optional[List[ProxyFile]]`
- Output: `content: str`

**Configuration (`config.json`):**

| Key | Default | Description |
|-----|---------|-------------|
| `LLM_AGENT_ID` | `llm_agent` | Agent for LLM inference |
| `DEFAULT_MODEL_ID` | `""` | Default model config key |
| `EMBED_BASE_URL` | `""` | Embedding API endpoint |
| `EMBED_MODEL` | `""` | Embedding model name |
| `EMBED_TIMEOUT` | `30` | Embedding request timeout (seconds) |
| `VECTOR_DIM` | `2560` | Embedding vector dimensions |
| `CHUNK_LEN_MAX` | `2000` | Max chunk size (characters) |
| `CHUNK_LEN_MIN` | `1000` | Min chunk size (characters) |
| `CHUNK_OVERLAP` | `100` | Overlap between chunks (characters) |
| `MD_CONVERTER_ID` | `md_converter` | Agent for document conversion |
| `TOOL_TIMEOUT` | `120` | Tool execution timeout (seconds) |

---

### mcp_agent

**Group:** `tool`

Outbound MCP gateway. Connects to external MCP tool servers and exposes their tools to the router ecosystem.

**Features:**
- Connects to MCP servers via stdio or SSE transport
- Dynamically discovers tools from connected MCP servers
- Exposes discovered tools to other agents via the router
- LLM-driven tool selection and execution
- Agent info is dynamically generated based on available MCP tools

**Input/Output:**
- Input/Output: Dynamically derived from connected MCP server tools

**Configuration (`config.json`):**

| Key | Default | Description |
|-----|---------|-------------|
| `LLM_AGENT_ID` | `llm_agent` | Agent for LLM inference |
| `LLM_MODEL_ID` | `""` | Model config key |
| `servers` | `[]` | List of MCP server configurations |

**Server configuration:**

```json
{
    "servers": [
        {
            "name": "my-mcp-server",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        }
    ]
}
```

---

### mcp_server

**Group:** `bridge` | **Hidden:** Yes

Inbound MCP bridge. Exposes router agents as MCP tools to external MCP clients (Claude Desktop, Cursor, etc.).

**Features:**
- Runs an MCP protocol server (stdio/SSE)
- Automatically generates MCP tool definitions from router's available agents
- Routes MCP tool calls to the appropriate agent via the router
- Configurable agent exclusion list

**Input/Output:**
- Input: `llmdata: LLMData`
- Output: `content: str`

**Configuration (`config.json`):**

| Key | Default | Description |
|-----|---------|-------------|
| `exclude_agents` | `[]` | Agent IDs to exclude from MCP exposure |

---

### web_admin

**Group:** `admin` | **Hidden:** Yes

Admin web frontend for system management.

**Features:**
- Agent management: view registered agents, update groups, disconnect
- Task management: list, filter, and inspect tasks
- Direct messaging: send test messages to agents
- Configuration editor: edit embedded agent `config.json` via the UI
- Invitation management: create and manage onboarding tokens
- Agent documentation viewer/editor
- System monitoring: agent health status
- Password-protected admin session

**No `config.json`** — web_admin reads ADMIN_TOKEN from its environment for router API access.

**Environment variables (`.env`):**

| Variable | Description |
|----------|-------------|
| `ROUTER_URL` | Router base URL |
| `ADMIN_TOKEN` | Router admin API token |
| `ADMIN_PASSWORD` | Web UI login password |
| `AGENT_PORT` | Listen port (default 8080) |
| `SESSION_SECRET` | Cookie signing secret |
