#!/usr/bin/env bash
# start.sh — Launch the Coding Agent
#
# Handles both bare-metal and Docker deployments:
# - Bootstraps data/.env from .env.example and data/config.json from
#   config.default.json if missing
# - Propagates env vars (from start.config via docker env_file) into data/.env
# - Starts the agent

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

DATA_DIR="${DATA_DIR:-data}"
ENV_FILE="$DATA_DIR/.env"
CFG_FILE="$DATA_DIR/config.json"
mkdir -p "$DATA_DIR"

# Bootstrap data/.env from .env.example if missing
if [ ! -f "$ENV_FILE" ] && [ -f ".env.example" ]; then
    cp ".env.example" "$ENV_FILE"
    echo "[coding] Created $ENV_FILE from .env.example"
fi

# Bootstrap data/config.json from config.default.json if missing
if [ ! -f "$CFG_FILE" ] && [ -f "config.default.json" ]; then
    cp "config.default.json" "$CFG_FILE"
    echo "[coding] Created $CFG_FILE from config.default.json"
fi

# Helper: set key=value in .env file
_set_env() {
    local file="$1" key="$2" value="$3"
    [ -z "$value" ] && return
    if grep -q "^${key}=" "$file" 2>/dev/null; then
        python3 -c "
import sys, pathlib
f, k, v = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
lines = f.read_text('utf-8').splitlines()
f.write_text('\n'.join(f'{k}={v}' if l.startswith(f'{k}=') else l for l in lines) + '\n', 'utf-8')
" "$file" "$key" "$value"
    else
        echo "${key}=${value}" >> "$file"
    fi
}

# Propagate secrets/infrastructure from env (docker env_file → data/.env)
_set_env "$ENV_FILE" "ADMIN_PASSWORD" "${ADMIN_PASSWORD:-}"
_set_env "$ENV_FILE" "AGENT_PORT" "${AGENT_PORT:-${CODING_PORT:-8100}}"
_set_env "$ENV_FILE" "ROUTER_URL" "${ROUTER_URL:-http://localhost:8000}"
_set_env "$ENV_FILE" "AGENT_URL" "${AGENT_URL:-}"

# Generate SESSION_SECRET if not already in .env
if ! grep -q '^SESSION_SECRET=.' "$ENV_FILE" 2>/dev/null; then
    _set_env "$ENV_FILE" "SESSION_SECRET" "$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
fi

# Load the bootstrapped .env
set -a
source "$ENV_FILE"
set +a

# Ensure helper.py is accessible
PARENT_DIR="$(dirname "$SCRIPT_DIR")"
if [ -f "$PARENT_DIR/helper.py" ]; then
    export PYTHONPATH="${PARENT_DIR}:${PYTHONPATH:-}"
fi

# Create workspace directories
mkdir -p "${WORKSPACE_ROOT:-data/workspaces}" "${LOG_DIR:-data/logs}"

echo "[coding] Starting on ${AGENT_HOST:-0.0.0.0}:${AGENT_PORT:-8100} ..."
exec .venv/bin/python3 agent.py
