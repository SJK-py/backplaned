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
        echo "[start] ERROR: $ROOT/.env not found and no .env.example to copy from." >&2
        exit 1
    fi
fi
set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a

# ── Load start.config (user settings — takes precedence over .env) ─────
if [ -f "$ROOT/start.config" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$ROOT/start.config"
    set +a
fi

# ── Propagate start.config values into agent configs ────────────────────

# Helper: set a key=value in an .env file (create key if missing)
set_env_var() {
    local file="$1" key="$2" value="$3"
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

# ── Bootstrap config.json from defaults if missing ─────────────────────
for default_cfg in "$ROOT"/agents/*/config.default.json; do
    agent_dir="$(dirname "$default_cfg")"
    cfg="$agent_dir/config.json"
    if [ ! -f "$cfg" ]; then
        cp "$default_cfg" "$cfg"
    fi
done

# Propagate ADMIN_TOKEN to root .env and web_admin
[ -n "$ADMIN_TOKEN" ] && set_env_var "$ROOT/.env" "ADMIN_TOKEN" "$ADMIN_TOKEN"
[ -n "$ADMIN_TOKEN" ] && set_env_var "$ROOT/agents_external/web_admin/.env" "ROUTER_ADMIN_TOKEN" "$ADMIN_TOKEN"

# Propagate ADMIN_PASSWORD to all agent .env files
if [ -n "$ADMIN_PASSWORD" ]; then
    for agent_env in \
        "$ROOT/agents_external/channel_agent/.env" \
        "$ROOT/agents_external/coding_agent/.env" \
        "$ROOT/agents_external/cron_agent/.env" \
        "$ROOT/agents_external/kb_agent/.env" \
        "$ROOT/agents_external/mcp_agent/.env" \
        "$ROOT/agents_external/mcp_server/.env" \
        "$ROOT/agents_external/reminder_agent/.env" \
        "$ROOT/agents_external/web_admin/.env"; do
        set_env_var "$agent_env" "ADMIN_PASSWORD" "$ADMIN_PASSWORD"
    done
fi

# Generate and propagate SESSION_SECRET (shared across all agents)
SESSION_SECRET="${SESSION_SECRET:-$("$VENV_BIN/python3" -c 'import secrets; print(secrets.token_hex(32))')}"
for agent_env in \
    "$ROOT/agents_external/channel_agent/.env" \
    "$ROOT/agents_external/coding_agent/.env" \
    "$ROOT/agents_external/cron_agent/.env" \
    "$ROOT/agents_external/kb_agent/.env" \
    "$ROOT/agents_external/mcp_agent/.env" \
    "$ROOT/agents_external/mcp_server/.env" \
    "$ROOT/agents_external/reminder_agent/.env" \
    "$ROOT/agents_external/web_admin/.env"; do
    set_env_var "$agent_env" "SESSION_SECRET" "$SESSION_SECRET"
done

# Propagate LLM Gateway settings to llm_agent config.json
if [ -n "$LLM_BASE_URL" ]; then
    LLM_CFG="$ROOT/agents/llm_agent/config.json"
    set_json_key "$LLM_CFG" "models.default.provider" "${LLM_PROVIDER:-openai_compat}"
    set_json_key "$LLM_CFG" "models.default.base_url" "$LLM_BASE_URL"
    set_json_key "$LLM_CFG" "models.default.api_key" "${LLM_API_KEY:-}"
    set_json_key "$LLM_CFG" "models.default.model" "$LLM_MODEL"
    [ -n "$LLM_MAX_TOKENS" ] && set_json_key "$LLM_CFG" "models.default.max_tokens" "$LLM_MAX_TOKENS"
fi

# Propagate memory_agent settings
MEM_CFG="$ROOT/agents/memory_agent/config.json"
[ -n "${MEM0_LLM_BASE_URL:-$LLM_BASE_URL}" ] && set_json_key "$MEM_CFG" "MEM0_LLM_BASE_URL" "${MEM0_LLM_BASE_URL:-$LLM_BASE_URL}"
[ -n "${MEM0_LLM_API_KEY:-$LLM_API_KEY}" ] && set_json_key "$MEM_CFG" "MEM0_LLM_API_KEY" "${MEM0_LLM_API_KEY:-$LLM_API_KEY}"
[ -n "${MEM0_LLM_MODEL:-$LLM_MODEL}" ] && set_json_key "$MEM_CFG" "MEM0_LLM_MODEL" "${MEM0_LLM_MODEL:-$LLM_MODEL}"
[ -n "$MEM0_EMBED_BASE_URL" ] && set_json_key "$MEM_CFG" "MEM0_EMBED_BASE_URL" "$MEM0_EMBED_BASE_URL"
[ -n "${MEM0_EMBED_API_KEY:-$LLM_API_KEY}" ] && set_json_key "$MEM_CFG" "MEM0_EMBED_API_KEY" "${MEM0_EMBED_API_KEY:-$LLM_API_KEY}"
[ -n "$MEM0_EMBED_MODEL" ] && set_json_key "$MEM_CFG" "MEM0_EMBED_MODEL" "$MEM0_EMBED_MODEL"
[ -n "$MEM0_EMBEDDING_DIMS" ] && set_json_key "$MEM_CFG" "MEM0_EMBEDDING_DIMS" "$MEM0_EMBEDDING_DIMS"
[ -n "$QDRANT_HOST" ] && set_json_key "$MEM_CFG" "MEM0_QDRANT_HOST" "$QDRANT_HOST"
[ -n "$QDRANT_PORT" ] && set_json_key "$MEM_CFG" "MEM0_QDRANT_PORT" "$QDRANT_PORT"

# Propagate md_converter OCR settings
if [ "$OCR_ENABLED" = "true" ]; then
    OCR_CFG="$ROOT/agents/md_converter/config.json"
    set_json_key "$OCR_CFG" "OCR_ENABLED" "true"
    set_json_key "$OCR_CFG" "OCR_BASE_URL" "${OCR_BASE_URL:-$LLM_BASE_URL}"
    [ -n "${OCR_API_KEY:-$LLM_API_KEY}" ] && set_json_key "$OCR_CFG" "OCR_API_KEY" "${OCR_API_KEY:-$LLM_API_KEY}"
    set_json_key "$OCR_CFG" "OCR_MODEL" "${OCR_MODEL:-$LLM_MODEL}"
fi

# Propagate kb_agent embedding settings
KB_ENV="$ROOT/agents_external/kb_agent/.env"
[ -n "${KB_EMBED_BASE_URL:-$MEM0_EMBED_BASE_URL}" ] && set_env_var "$KB_ENV" "EMBED_BASE_URL" "${KB_EMBED_BASE_URL:-$MEM0_EMBED_BASE_URL}"
[ -n "${KB_EMBED_API_KEY:-${MEM0_EMBED_API_KEY:-$LLM_API_KEY}}" ] && set_env_var "$KB_ENV" "EMBED_API_KEY" "${KB_EMBED_API_KEY:-${MEM0_EMBED_API_KEY:-$LLM_API_KEY}}"
[ -n "${KB_EMBED_MODEL:-$MEM0_EMBED_MODEL}" ] && set_env_var "$KB_ENV" "EMBED_MODEL" "${KB_EMBED_MODEL:-$MEM0_EMBED_MODEL}"
[ -n "${KB_VECTOR_DIM:-$MEM0_EMBEDDING_DIMS}" ] && set_env_var "$KB_ENV" "VECTOR_DIM" "${KB_VECTOR_DIM:-$MEM0_EMBEDDING_DIMS}"

# Propagate Telegram/Discord settings
CHAN_ENV="$ROOT/agents_external/channel_agent/.env"
[ -n "$TELEGRAM_TOKEN" ] && set_env_var "$CHAN_ENV" "TELEGRAM_TOKEN" "$TELEGRAM_TOKEN"
[ -n "$TELEGRAM_ALLOWED_IDS" ] && set_env_var "$CHAN_ENV" "TELEGRAM_ALLOWED_IDS" "$TELEGRAM_ALLOWED_IDS"
[ -n "$DISCORD_TOKEN" ] && set_env_var "$CHAN_ENV" "DISCORD_TOKEN" "$DISCORD_TOKEN"
[ -n "$DISCORD_ALLOWED_IDS" ] && set_env_var "$CHAN_ENV" "DISCORD_ALLOWED_IDS" "$DISCORD_ALLOWED_IDS"

# ── Ports (propagate to agent .env files) ────────────────────────────────
ROUTER_PORT="${ROUTER_PORT:-8000}"
WEB_ADMIN_PORT="${WEB_ADMIN_PORT:-8080}"
CHANNEL_PORT="${CHANNEL_PORT:-8081}"
MCP_AGENT_PORT="${MCP_AGENT_PORT:-8082}"
MCP_SERVER_PORT="${MCP_SERVER_PORT:-8083}"
CRON_PORT="${CRON_PORT:-8085}"
KB_PORT="${KB_PORT:-8086}"
CODING_PORT="${CODING_PORT:-8100}"
REMINDER_PORT="${REMINDER_PORT:-8101}"

set_env_var "$ROOT/agents_external/web_admin/.env" "AGENT_PORT" "$WEB_ADMIN_PORT"
set_env_var "$ROOT/agents_external/channel_agent/.env" "AGENT_PORT" "$CHANNEL_PORT"
set_env_var "$ROOT/agents_external/mcp_agent/.env" "AGENT_PORT" "$MCP_AGENT_PORT"
set_env_var "$ROOT/agents_external/mcp_server/.env" "AGENT_PORT" "$MCP_SERVER_PORT"
set_env_var "$ROOT/agents_external/cron_agent/.env" "AGENT_PORT" "$CRON_PORT"
set_env_var "$ROOT/agents_external/kb_agent/.env" "AGENT_PORT" "$KB_PORT"
set_env_var "$ROOT/agents_external/coding_agent/.env" "AGENT_PORT" "$CODING_PORT"
set_env_var "$ROOT/agents_external/reminder_agent/.env" "AGENT_PORT" "$REMINDER_PORT"

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

# ── Seed ACL group allowlist ─────────────────────────────────────────────
# (init_db seeds embedded->embedded; we add the full rule set here)
seed_acl() {
    local rules='[
        ["core","infra"],["core","tool"],["core","usertool"],["core","channel"],
        ["channel","core"],
        ["tool","infra"],
        ["usertool","infra"],["usertool","tool"],
        ["notify","core"],["notify","channel"],
        ["bridge","tool"],["bridge","infra"],
        ["admin","core"],["admin","tool"],["admin","usertool"],["admin","infra"],["admin","channel"]
    ]'
    "$VENV_BIN/python3" - "$rules" <<'PYEOF'
import sys, json, urllib.request
rules = json.loads(sys.argv[1])
token = __import__("os").environ.get("ADMIN_TOKEN", "")
base = f"http://localhost:{__import__('os').environ.get('ROUTER_PORT', '8000')}"
for outbound, inbound in rules:
    try:
        req = urllib.request.Request(
            f"{base}/admin/group-allowlist",
            data=json.dumps({"inbound_group": inbound, "outbound_group": outbound}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass
PYEOF
    echo "[start] ACL rules seeded."
}

seed_acl

# ── Web Admin ────────────────────────────────────────────────────────────
WEB_ADMIN_ENV="$ROOT/agents_external/web_admin/.env"
WEB_ADMIN_CREDS="$ROOT/agents_external/web_admin/data/credentials.json"

if [ ! -f "$WEB_ADMIN_CREDS" ]; then
    echo "[web-admin] No saved credentials — creating invitation token ..."
    create_invitation "web-admin" "$WEB_ADMIN_ENV" '["admin"]' '["admin"]' >/dev/null
fi

echo "[web-admin] Starting on http://0.0.0.0:${WEB_ADMIN_PORT} ..."
cd "$ROOT/agents_external/web_admin"
"$VENV_BIN/uvicorn" main:app --host 0.0.0.0 --port "$WEB_ADMIN_PORT" &
WEB_ADMIN_PID=$!
cd "$ROOT"

# ── Channel Agent ────────────────────────────────────────────────────────
CHAN_ENV="$ROOT/agents_external/channel_agent/.env"
CHAN_CREDS="$ROOT/agents_external/channel_agent/data/credentials.json"

if [ ! -f "$CHAN_CREDS" ]; then
    echo "[channel] No saved credentials — creating invitation token ..."
    create_invitation "channel" "$CHAN_ENV" '["channel"]' '["channel"]' >/dev/null
fi

echo "[channel] Starting on http://0.0.0.0:${CHANNEL_PORT} ..."
cd "$ROOT/agents_external/channel_agent"
"$VENV_BIN/uvicorn" main:app --host 0.0.0.0 --port "$CHANNEL_PORT" &
CHANNEL_PID=$!
cd "$ROOT"

# ── MCP Server ───────────────────────────────────────────────────────────
MCP_SERVER_ENV="$ROOT/agents_external/mcp_server/.env"
MCP_SERVER_CREDS="$ROOT/agents_external/mcp_server/data/credentials.json"

if [ ! -f "$MCP_SERVER_CREDS" ]; then
    echo "[mcp-server] No saved credentials — creating invitation token ..."
    create_invitation "mcp-server" "$MCP_SERVER_ENV" '["bridge"]' '["bridge"]' >/dev/null
fi

echo "[mcp-server] Starting on http://0.0.0.0:${MCP_SERVER_PORT} ..."
cd "$ROOT/agents_external/mcp_server"
"$VENV_BIN/uvicorn" main:app --host 0.0.0.0 --port "$MCP_SERVER_PORT" &
MCP_SERVER_PID=$!
cd "$ROOT"

# ── MCP Agent ────────────────────────────────────────────────────────────
MCP_AGENT_ENV="$ROOT/agents_external/mcp_agent/.env"
MCP_AGENT_CREDS="$ROOT/agents_external/mcp_agent/data/credentials.json"

if [ ! -f "$MCP_AGENT_CREDS" ]; then
    echo "[mcp-agent] No saved credentials — creating invitation token ..."
    create_invitation "mcp-agent" "$MCP_AGENT_ENV" '["tool"]' '["tool"]' >/dev/null
fi

echo "[mcp-agent] Starting on http://0.0.0.0:${MCP_AGENT_PORT} ..."
cd "$ROOT/agents_external/mcp_agent"
"$VENV_BIN/uvicorn" main:app --host 0.0.0.0 --port "$MCP_AGENT_PORT" &
MCP_AGENT_PID=$!
cd "$ROOT"

# ── Coding Agent ─────────────────────────────────────────────────────────
CODING_ENV="$ROOT/agents_external/coding_agent/.env"
CODING_CREDS="$ROOT/agents_external/coding_agent/data/credentials.json"

if [ ! -f "$CODING_CREDS" ]; then
    echo "[coding] No saved credentials — creating invitation token ..."
    create_invitation "coding" "$CODING_ENV" '["usertool"]' '["usertool"]' >/dev/null
fi

echo "[coding] Starting on http://0.0.0.0:${CODING_PORT} ..."
cd "$ROOT/agents_external/coding_agent"
PYTHONPATH="$ROOT:${PYTHONPATH:-}" "$VENV_BIN/python3" agent.py &
CODING_PID=$!
cd "$ROOT"

# ── Reminder Agent ───────────────────────────────────────────────────────
REMINDER_ENV="$ROOT/agents_external/reminder_agent/.env"
REMINDER_CREDS="$ROOT/agents_external/reminder_agent/data/credentials.json"

if [ ! -f "$REMINDER_CREDS" ]; then
    echo "[reminder] No saved credentials — creating invitation token ..."
    create_invitation "reminder" "$REMINDER_ENV" '["usertool"]' '["usertool","notify"]' >/dev/null
fi

echo "[reminder] Starting on http://0.0.0.0:${REMINDER_PORT} ..."
cd "$ROOT/agents_external/reminder_agent"
PYTHONPATH="$ROOT:${PYTHONPATH:-}" "$VENV_BIN/python3" agent.py &
REMINDER_PID=$!
cd "$ROOT"

# ── Cron Agent ───────────────────────────────────────────────────────────
CRON_ENV="$ROOT/agents_external/cron_agent/.env"
CRON_CREDS="$ROOT/agents_external/cron_agent/data/credentials.json"

if [ ! -f "$CRON_CREDS" ]; then
    echo "[cron] No saved credentials — creating invitation token ..."
    create_invitation "cron" "$CRON_ENV" '["usertool"]' '["usertool","notify"]' >/dev/null
fi

echo "[cron] Starting on http://0.0.0.0:${CRON_PORT} ..."
cd "$ROOT/agents_external/cron_agent"
PYTHONPATH="$ROOT:${PYTHONPATH:-}" "$VENV_BIN/python3" agent.py &
CRON_PID=$!
cd "$ROOT"

# ── KB Agent ─────────────────────────────────────────────────────────────
KB_ENV="$ROOT/agents_external/kb_agent/.env"
KB_CREDS="$ROOT/agents_external/kb_agent/data/credentials.json"

if [ ! -f "$KB_CREDS" ]; then
    echo "[kb] No saved credentials — creating invitation token ..."
    create_invitation "kb" "$KB_ENV" '["usertool"]' '["usertool"]' >/dev/null
fi

echo "[kb] Starting on http://0.0.0.0:${KB_PORT} ..."
cd "$ROOT/agents_external/kb_agent"
PYTHONPATH="$ROOT:${PYTHONPATH:-}" "$VENV_BIN/python3" agent.py &
KB_PID=$!
cd "$ROOT"

# ── Shutdown trap ────────────────────────────────────────────────────────
ALL_PIDS="$ROUTER_PID $WEB_ADMIN_PID $CHANNEL_PID $MCP_SERVER_PID $MCP_AGENT_PID $CODING_PID $REMINDER_PID $CRON_PID $KB_PID"
echo ""
echo "[start] ─── All services running ───"
echo "[start]   Router          PID=$ROUTER_PID      http://localhost:${ROUTER_PORT}"
echo "[start]   Web Admin       PID=$WEB_ADMIN_PID   http://localhost:${WEB_ADMIN_PORT}"
echo "[start]   Channel Agent   PID=$CHANNEL_PID     http://localhost:${CHANNEL_PORT}"
echo "[start]   MCP Server      PID=$MCP_SERVER_PID  http://localhost:${MCP_SERVER_PORT}"
echo "[start]   MCP Agent       PID=$MCP_AGENT_PID   http://localhost:${MCP_AGENT_PORT}"
echo "[start]   Coding Agent    PID=$CODING_PID      http://localhost:${CODING_PORT}"
echo "[start]   Reminder Agent  PID=$REMINDER_PID    http://localhost:${REMINDER_PORT}"
echo "[start]   Cron Agent      PID=$CRON_PID        http://localhost:${CRON_PORT}"
echo "[start]   KB Agent        PID=$KB_PID          http://localhost:${KB_PORT}"
echo "[start] Press Ctrl+C to stop all."
echo ""

_shutdown() {
    echo "[stop] Shutting down..."
    kill $ALL_PIDS 2>/dev/null
    wait
    exit 0
}
trap _shutdown SIGINT SIGTERM

# Monitor child processes — log which service died instead of silently exiting
set +e
while true; do
    wait -n -p EXITED_PID $ALL_PIDS 2>/dev/null
    EXIT_CODE=$?
    # Identify which service exited
    for name_pid in \
        "Router:$ROUTER_PID" "Web Admin:$WEB_ADMIN_PID" "Channel Agent:$CHANNEL_PID" \
        "MCP Server:$MCP_SERVER_PID" "MCP Agent:$MCP_AGENT_PID" "Coding Agent:$CODING_PID" \
        "Reminder Agent:$REMINDER_PID" "Cron Agent:$CRON_PID" "KB Agent:$KB_PID"; do
        svc="${name_pid%%:*}"
        pid="${name_pid##*:}"
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "[start] WARNING: $svc (PID=$pid) exited with code $EXIT_CODE"
        fi
    done
    # If the router itself died, shut everything down
    if ! kill -0 "$ROUTER_PID" 2>/dev/null; then
        echo "[start] ERROR: Router has exited — shutting down all services."
        kill $ALL_PIDS 2>/dev/null
        wait
        exit 1
    fi
done
