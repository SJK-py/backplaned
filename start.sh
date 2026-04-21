#!/usr/bin/env bash
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV_BIN="$ROOT/.venv/$([ -d "$ROOT/.venv/Scripts" ] && echo Scripts || echo bin)"

# ── Load root .env (router defaults — loaded first so start.config wins) ─
if [ ! -f "$ROOT/.env" ]; then
    if [ -f "$ROOT/.env.example" ]; then
        cp "$ROOT/.env.example" "$ROOT/.env"
        echo "[start] Created .env from .env.example"
    else
        # In Docker, .env is not needed — env vars come from env_file.
        echo "[start] No .env found (OK in Docker — using environment variables)"
    fi
fi
if [ -f "$ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$ROOT/.env"
    set +a
fi

# ── Load start.config (user settings — takes precedence over .env) ─────
if [ -f "$ROOT/start.config" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$ROOT/start.config"
    set +a
fi

# ── Parse EXCLUDE_AGENTS list ─────────────────────────────────────────
# Comma-separated list of external agent directory names to skip during
# bootstrap and startup.  E.g. EXCLUDE_AGENTS="coding_agent,kb_agent"
IFS=',' read -ra _EXCLUDED_AGENTS <<< "${EXCLUDE_AGENTS:-}"
is_excluded() {
    local agent="$1"
    for ex in "${_EXCLUDED_AGENTS[@]}"; do
        # Trim whitespace
        ex="$(echo "$ex" | tr -d '[:space:]')"
        [ "$ex" = "$agent" ] && return 0
    done
    return 1
}
if [ -n "$EXCLUDE_AGENTS" ]; then
    echo "[start] Excluded agents: $EXCLUDE_AGENTS"
fi

# ── Helpers ────────────────────────────────────────────────────────────

# Helper: set a key=value in an .env file (create key if missing)
set_env_var() {
    local file="$1" key="$2" value="$3"
    mkdir -p "$(dirname "$file")"
    if grep -q "^${key}=" "$file" 2>/dev/null; then
        "$VENV_BIN/python3" - "$file" "$key" "$value" <<'PYEOF'
import sys, pathlib
f, key, val = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
lines = f.read_text(encoding="utf-8").splitlines()
f.write_text("\n".join(
    f"{key}={val}" if l.startswith(f"{key}=") else l for l in lines
) + "\n", encoding="utf-8")
PYEOF
    else
        echo "${key}=${value}" >> "$file"
    fi
}

# Helper: update JSON config key (creates file if missing)
set_json_key() {
    local file="$1" key="$2" value="$3"
    mkdir -p "$(dirname "$file")"
    "$VENV_BIN/python3" - "$file" "$key" "$value" <<'PYEOF'
import sys, json, pathlib
f = pathlib.Path(sys.argv[1])
key, val = sys.argv[2], sys.argv[3]
data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
# Navigate dotted keys: "models.default.base_url" -> data["models"]["default"]["base_url"]
parts = key.split(".")
obj = data
for p in parts[:-1]:
    obj = obj.setdefault(p, {})
# Try to preserve type (int, float, bool, null)
for convert in [int, float]:
    try: val = convert(val); break
    except ValueError: pass
else:
    if val.lower() == "true": val = True
    elif val.lower() == "false": val = False
    elif val.lower() == "null": val = None
obj[parts[-1]] = val
f.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
PYEOF
}

# ── Bootstrap config files to data/ dirs ──────────────────────────────
# Only create if missing — never overwrite user-modified configs.

# Embedded agents: set/clear .ignore_agent flag based on EXCLUDE_AGENTS.
# The router skips loading any agent dir that contains .ignore_agent.
for agent_dir in "$ROOT"/agents/*/; do
    [ -d "$agent_dir" ] || continue
    agent_name="$(basename "$agent_dir")"
    if is_excluded "$agent_name"; then
        touch "$agent_dir/.ignore_agent"
        echo "[start] Excluded embedded agent: $agent_name"
    else
        rm -f "$agent_dir/.ignore_agent"
    fi
done

# Embedded agents: config.default.json → data/config.json
for default_cfg in "$ROOT"/agents/*/config.default.json; do
    agent_dir="$(dirname "$default_cfg")"
    data_cfg="$agent_dir/data/config.json"
    mkdir -p "$agent_dir/data"
    if [ ! -f "$data_cfg" ]; then
        cp "$default_cfg" "$data_cfg"
        echo "[start] Created $(basename "$agent_dir")/data/config.json from defaults"
    fi
done

# External agents: .env.example → data/.env, config.default.json → data/config.json
for agent_dir in "$ROOT"/agents_external/*/; do
    agent_name="$(basename "$agent_dir")"
    is_excluded "$agent_name" && continue

    # .env
    data_env="$agent_dir/data/.env"
    mkdir -p "$agent_dir/data"
    if [ ! -f "$data_env" ]; then
        if [ -f "$agent_dir/.env.example" ]; then
            cp "$agent_dir/.env.example" "$data_env"
            echo "[start] Created $agent_name/data/.env from .env.example"
        fi
    fi

    # config.json (if a config.default.json template exists)
    if [ -f "$agent_dir/config.default.json" ]; then
        data_cfg="$agent_dir/data/config.json"
        if [ ! -f "$data_cfg" ]; then
            cp "$agent_dir/config.default.json" "$data_cfg"
            echo "[start] Created $agent_name/data/config.json from defaults"
        fi
    fi
done

# ── Propagate start.config values into agent configs ──────────────────
# These only run on first boot (or when FORCE_CONFIG=1 is set) because
# the bootstrap step above creates the config files.  On subsequent
# boots the files already exist with user modifications.

# Propagate ADMIN_TOKEN to root .env and web_admin
[ -n "$ADMIN_TOKEN" ] && set_env_var "$ROOT/.env" "ADMIN_TOKEN" "$ADMIN_TOKEN"
[ -n "$ADMIN_TOKEN" ] && ! is_excluded "web_admin" && set_env_var "$ROOT/agents_external/web_admin/data/.env" "ROUTER_ADMIN_TOKEN" "$ADMIN_TOKEN"

# Propagate ADMIN_PASSWORD to all agent .env files
if [ -n "$ADMIN_PASSWORD" ]; then
    for _agent_name in channel_agent coding_agent cron_agent kb_agent mcp_agent mcp_server reminder_agent web_admin; do
        is_excluded "$_agent_name" && continue
        set_env_var "$ROOT/agents_external/$_agent_name/data/.env" "ADMIN_PASSWORD" "$ADMIN_PASSWORD"
    done
fi

# Generate and propagate SESSION_SECRET (shared across all agents)
SESSION_SECRET="${SESSION_SECRET:-$("$VENV_BIN/python3" -c 'import secrets; print(secrets.token_hex(32))')}"
for _agent_name in channel_agent coding_agent cron_agent kb_agent mcp_agent mcp_server reminder_agent web_admin; do
    is_excluded "$_agent_name" && continue
    set_env_var "$ROOT/agents_external/$_agent_name/data/.env" "SESSION_SECRET" "$SESSION_SECRET"
done

# Propagate LLM Gateway settings to llm_agent data/config.json
if [ -n "$LLM_BASE_URL" ]; then
    LLM_CFG="$ROOT/agents/llm_agent/data/config.json"
    set_json_key "$LLM_CFG" "models.default.provider" "${LLM_PROVIDER:-openai_compat}"
    set_json_key "$LLM_CFG" "models.default.base_url" "$LLM_BASE_URL"
    set_json_key "$LLM_CFG" "models.default.api_key" "${LLM_API_KEY:-}"
    set_json_key "$LLM_CFG" "models.default.model" "$LLM_MODEL"
    [ -n "$LLM_MAX_TOKENS" ] && set_json_key "$LLM_CFG" "models.default.max_tokens" "$LLM_MAX_TOKENS"
fi

# Propagate memory_agent settings
MEM_CFG="$ROOT/agents/memory_agent/data/config.json"
[ -n "$MEMORY_EMBED_BASE_URL" ] && set_json_key "$MEM_CFG" "EMBED_BASE_URL" "$MEMORY_EMBED_BASE_URL"
[ -n "${MEMORY_EMBED_API_KEY:-$LLM_API_KEY}" ] && set_json_key "$MEM_CFG" "EMBED_API_KEY" "${MEMORY_EMBED_API_KEY:-$LLM_API_KEY}"
[ -n "$MEMORY_EMBED_MODEL" ] && set_json_key "$MEM_CFG" "EMBED_MODEL" "$MEMORY_EMBED_MODEL"
[ -n "$MEMORY_EMBEDDING_DIMS" ] && set_json_key "$MEM_CFG" "EMBEDDING_DIMS" "$MEMORY_EMBEDDING_DIMS"

# Propagate md_converter OCR settings
if [ "$OCR_ENABLED" = "true" ]; then
    OCR_CFG="$ROOT/agents/md_converter/data/config.json"
    set_json_key "$OCR_CFG" "OCR_ENABLED" "true"
    set_json_key "$OCR_CFG" "OCR_BASE_URL" "${OCR_BASE_URL:-$LLM_BASE_URL}"
    [ -n "${OCR_API_KEY:-$LLM_API_KEY}" ] && set_json_key "$OCR_CFG" "OCR_API_KEY" "${OCR_API_KEY:-$LLM_API_KEY}"
    set_json_key "$OCR_CFG" "OCR_MODEL" "${OCR_MODEL:-$LLM_MODEL}"
fi

# Propagate web_agent search settings
WEB_CFG="$ROOT/agents/web_agent/data/config.json"
[ -n "$SEARCH_PROVIDER" ] && set_json_key "$WEB_CFG" "SEARCH_PROVIDER" "$SEARCH_PROVIDER"
[ -n "$SEARXNG_BASE_URL" ] && set_json_key "$WEB_CFG" "SEARXNG_BASE_URL" "$SEARXNG_BASE_URL"
[ -n "$BRAVE_API_KEY" ] && set_json_key "$WEB_CFG" "BRAVE_API_KEY" "$BRAVE_API_KEY"

# Propagate kb_agent embedding settings to data/config.json
if ! is_excluded "kb_agent"; then
    KB_CFG="$ROOT/agents_external/kb_agent/data/config.json"
    [ -n "${KB_EMBED_BASE_URL:-$MEMORY_EMBED_BASE_URL}" ] && set_json_key "$KB_CFG" "EMBED_BASE_URL" "${KB_EMBED_BASE_URL:-$MEMORY_EMBED_BASE_URL}"
    [ -n "${KB_EMBED_MODEL:-$MEMORY_EMBED_MODEL}" ] && set_json_key "$KB_CFG" "EMBED_MODEL" "${KB_EMBED_MODEL:-$MEMORY_EMBED_MODEL}"
    [ -n "${KB_VECTOR_DIM:-$MEMORY_EMBEDDING_DIMS}" ] && set_json_key "$KB_CFG" "VECTOR_DIM" "${KB_VECTOR_DIM:-$MEMORY_EMBEDDING_DIMS}"
    # API key is a secret → goes in .env
    [ -n "${KB_EMBED_API_KEY:-${MEMORY_EMBED_API_KEY:-$LLM_API_KEY}}" ] && set_env_var "$ROOT/agents_external/kb_agent/data/.env" "EMBED_API_KEY" "${KB_EMBED_API_KEY:-${MEMORY_EMBED_API_KEY:-$LLM_API_KEY}}"
fi

# Propagate Telegram/Discord tokens (secrets → .env)
if ! is_excluded "channel_agent"; then
    CHAN_ENV="$ROOT/agents_external/channel_agent/data/.env"
    [ -n "$TELEGRAM_TOKEN" ] && set_env_var "$CHAN_ENV" "TELEGRAM_TOKEN" "$TELEGRAM_TOKEN"
    [ -n "$DISCORD_TOKEN" ] && set_env_var "$CHAN_ENV" "DISCORD_TOKEN" "$DISCORD_TOKEN"
    # Rate limits → config.json
    CHAN_CFG="$ROOT/agents_external/channel_agent/data/config.json"
    [ -n "$RATE_LIMIT_WINDOW" ] && set_json_key "$CHAN_CFG" "RATE_LIMIT_WINDOW" "$RATE_LIMIT_WINDOW"
    [ -n "$RATE_LIMIT_MAX_TRIALS" ] && set_json_key "$CHAN_CFG" "RATE_LIMIT_MAX_TRIALS" "$RATE_LIMIT_MAX_TRIALS"
fi

# ── Ports (propagate to agent data/.env files) ──────────────────────────
ROUTER_PORT="${ROUTER_PORT:-8000}"
WEB_ADMIN_PORT="${WEB_ADMIN_PORT:-8080}"
CHANNEL_PORT="${CHANNEL_PORT:-8081}"
MCP_AGENT_PORT="${MCP_AGENT_PORT:-8082}"
MCP_SERVER_PORT="${MCP_SERVER_PORT:-8083}"
CRON_PORT="${CRON_PORT:-8085}"
KB_PORT="${KB_PORT:-8086}"
CODING_PORT="${CODING_PORT:-8100}"
REMINDER_PORT="${REMINDER_PORT:-8101}"

# Helper: set AGENT_PORT and AGENT_URL for an external agent
_set_agent_port() {
    local agent_name="$1" port="$2"
    is_excluded "$agent_name" && return
    local env_file="$ROOT/agents_external/$agent_name/data/.env"
    set_env_var "$env_file" "AGENT_PORT" "$port"
    set_env_var "$env_file" "AGENT_URL" "http://localhost:${port}"
}

_set_agent_port "web_admin"      "$WEB_ADMIN_PORT"
_set_agent_port "channel_agent"  "$CHANNEL_PORT"
_set_agent_port "mcp_agent"      "$MCP_AGENT_PORT"
_set_agent_port "mcp_server"     "$MCP_SERVER_PORT"
_set_agent_port "cron_agent"     "$CRON_PORT"
_set_agent_port "kb_agent"       "$KB_PORT"
_set_agent_port "coding_agent"   "$CODING_PORT"
_set_agent_port "reminder_agent" "$REMINDER_PORT"

# MCP protocol port (separate from mcp_server's AGENT_PORT)
MCP_PORT="${MCP_PORT:-8084}"
is_excluded "mcp_server" || set_env_var "$ROOT/agents_external/mcp_server/data/.env" "MCP_PORT" "$MCP_PORT"

# ── Re-source root .env (may have been updated above) ───────────────────
set -a
source "$ROOT/.env"
set +a

# ── Router ──────────────────────────────────────────────────────────────
echo "[router] Starting on http://0.0.0.0:${ROUTER_PORT} ..."
"$VENV_BIN/uvicorn" router:app --host 0.0.0.0 --port "$ROUTER_PORT" &
ROUTER_PID=$!

# ── Wait for router to be ready ─────────────────────────────────────────
echo "[start] Waiting for router ..."
ROUTER_UP=0
for i in $(seq 1 30); do
    if curl -sf "http://localhost:${ROUTER_PORT}/health" >/dev/null 2>&1; then
        echo "[start] Router is up."
        ROUTER_UP=1
        break
    fi
    sleep 1
done
if [ "$ROUTER_UP" -eq 0 ]; then
    echo "[start] ERROR: Router did not start within 30 s. Aborting."
    kill "$ROUTER_PID" 2>/dev/null
    exit 1
fi

# ── Helper: create invitation token with specific groups ─────────────────
create_invitation() {
    local label="$1" env_file="$2" inbound="$3" outbound="$4"
    local token_resp inv_token
    token_resp=$(curl -sf -X POST "http://localhost:${ROUTER_PORT}/admin/invitation" \
        -H "Authorization: Bearer ${ADMIN_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"inbound_groups\":${inbound},\"outbound_groups\":${outbound}}")
    inv_token=$(echo "$token_resp" | "$VENV_BIN/python3" -c "import sys,json; print(json.load(sys.stdin)['token'])")
    if [ -n "$inv_token" ]; then
        echo "[$label] Got invitation token" >&2
        set_env_var "$env_file" "INVITATION_TOKEN" "$inv_token"
        echo "$inv_token"
    else
        echo "[$label] WARNING: failed to create invitation token." >&2
        echo ""
    fi
}

# ── Helper: validate credentials.json against the running router ─────────
# Probes /agent/destinations with the saved auth_token.  On 200 returns 0
# (credentials are valid).  On any non-200 HTTP response (typically 403
# "Invalid credentials" after a rebuild that wiped router.db) removes
# credentials.json and clears AGENT_AUTH_TOKEN from data/.env so the
# existing re-onboarding path in start_agent() fires automatically.
# The router has already passed its health check at this point, so any
# non-200 is treated as stale credentials, not a network hiccup.
validate_credentials() {
    local label="$1" creds_file="$2" env_file="$3"

    [ -f "$creds_file" ] || return 0

    local auth_token
    auth_token=$("$VENV_BIN/python3" - "$creds_file" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        print(json.load(f).get("auth_token", ""))
except Exception:
    pass
PYEOF
)

    if [ -z "$auth_token" ]; then
        echo "[$label] credentials.json missing auth_token — clearing for re-onboarding."
        rm -f "$creds_file"
        [ -f "$env_file" ] && set_env_var "$env_file" "AGENT_AUTH_TOKEN" ""
        return 0
    fi

    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
        -H "Authorization: Bearer $auth_token" \
        "http://localhost:${ROUTER_PORT}/agent/destinations" 2>/dev/null || echo "000")

    if [ "$http_code" = "200" ]; then
        return 0
    fi

    echo "[$label] Router rejected saved credentials (HTTP $http_code) — clearing for re-onboarding."
    rm -f "$creds_file"
    [ -f "$env_file" ] && set_env_var "$env_file" "AGENT_AUTH_TOKEN" ""
    return 0
}

# ── Helper: start an external agent (skips if excluded) ────────────────
# Usage: start_agent <dir_name> <label> <inbound_groups> <outbound_groups> <port> <start_cmd...>
#   dir_name:        directory name under agents_external/
#   label:           display name for log messages
#   inbound_groups:  JSON array string for invitation, e.g. '["tool"]'
#   outbound_groups: JSON array string for invitation
#   port:            port number to bind on
#   start_cmd...:    command and args to launch the agent
#
# Appends to RUNNING_PIDS and RUNNING_SERVICES for shutdown tracking.

RUNNING_PIDS="$ROUTER_PID"
RUNNING_SERVICES="Router:$ROUTER_PID"

start_agent() {
    local dir_name="$1" label="$2" inbound="$3" outbound="$4" port="$5"
    shift 5  # remaining args are the start command

    if is_excluded "$dir_name"; then
        echo "[$label] Skipped (excluded via EXCLUDE_AGENTS)"
        return 0
    fi

    local agent_env="$ROOT/agents_external/$dir_name/data/.env"
    local agent_creds="$ROOT/agents_external/$dir_name/data/credentials.json"

    # Validate any saved credentials against the router; stale ones
    # (e.g. after a router.db reset) are deleted so the branch below
    # fires and mints a fresh invitation token.
    validate_credentials "$label" "$agent_creds" "$agent_env"

    if [ ! -f "$agent_creds" ]; then
        echo "[$label] No saved credentials — creating invitation token ..."
        create_invitation "$label" "$agent_env" "$inbound" "$outbound" >/dev/null
    fi

    echo "[$label] Starting on http://0.0.0.0:${port} ..."
    cd "$ROOT/agents_external/$dir_name"
    "$@" &
    local pid=$!
    cd "$ROOT"

    RUNNING_PIDS="$RUNNING_PIDS $pid"
    RUNNING_SERVICES="$RUNNING_SERVICES $label:$pid"
}

# ── External Agents ────────────────────────────────────────────────────

# Ensure helper.py and config_ui.py (at project root) are importable by all agents.
PYTHONPATH="$ROOT:${PYTHONPATH:-}"
export PYTHONPATH

start_agent "web_admin" "Web-Admin" '["admin"]' '["admin"]' "$WEB_ADMIN_PORT" \
    "$VENV_BIN/uvicorn" main:app --host 0.0.0.0 --port "$WEB_ADMIN_PORT"

start_agent "channel_agent" "Channel" '["channel"]' '["channel"]' "$CHANNEL_PORT" \
    "$VENV_BIN/uvicorn" main:app --host 0.0.0.0 --port "$CHANNEL_PORT"

start_agent "mcp_server" "MCP-Server" '["bridge"]' '["bridge"]' "$MCP_SERVER_PORT" \
    "$VENV_BIN/uvicorn" main:app --host 0.0.0.0 --port "$MCP_SERVER_PORT"

start_agent "mcp_agent" "MCP-Agent" '["tool"]' '["tool"]' "$MCP_AGENT_PORT" \
    "$VENV_BIN/uvicorn" main:app --host 0.0.0.0 --port "$MCP_AGENT_PORT"

start_agent "coding_agent" "Coding" '["usertool"]' '["usertool"]' "$CODING_PORT" \
    "$VENV_BIN/python3" agent.py

start_agent "reminder_agent" "Reminder" '["usertool"]' '["usertool","notify"]' "$REMINDER_PORT" \
    "$VENV_BIN/python3" agent.py

start_agent "cron_agent" "Cron" '["usertool"]' '["usertool","notify"]' "$CRON_PORT" \
    "$VENV_BIN/python3" agent.py

start_agent "kb_agent" "KB" '["usertool"]' '["usertool"]' "$KB_PORT" \
    "$VENV_BIN/python3" agent.py

# ── Shutdown trap ────────────────────────────────────────────────────────
ALL_PIDS="$RUNNING_PIDS"
echo ""
echo "[start] ─── All services running ───"
echo "[start]   Router          PID=$ROUTER_PID      http://localhost:${ROUTER_PORT}"
for entry in $RUNNING_SERVICES; do
    svc="${entry%%:*}"
    pid="${entry##*:}"
    [ "$svc" = "Router" ] && continue
    printf "[start]   %-16s PID=%s\n" "$svc" "$pid"
done
echo "[start] Press Ctrl+C to stop all."
echo ""

_shutdown() {
    echo ""
    echo "[start] Shutting down ..."
    for pid in $ALL_PIDS; do
        kill "$pid" 2>/dev/null
    done
    wait
    echo "[start] All stopped."
    exit 0
}
trap _shutdown INT TERM

# Wait for any child to exit; if one dies, shut down all
while true; do
    for pid in $ALL_PIDS; do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "[start] Process $pid exited. Shutting down all ..."
            _shutdown
        fi
    done
    sleep 5
done
