#!/usr/bin/env bash
# start.sh — Launch the Coding Agent
#
# Usage:
#   ./start.sh          # Use .env in current directory
#   ./start.sh /path/to/.env   # Use specific .env file

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env
ENV_FILE="${1:-.env}"
if [ -f "$ENV_FILE" ]; then
    echo "Loading environment from $ENV_FILE"
    set -a
    source "$ENV_FILE"
    set +a
else
    echo "Warning: $ENV_FILE not found. Using existing environment variables."
fi

# Ensure helper.py is accessible
PARENT_DIR="$(dirname "$SCRIPT_DIR")"
if [ -f "$PARENT_DIR/helper.py" ]; then
    export PYTHONPATH="${PARENT_DIR}:${PYTHONPATH:-}"
fi

# Create directories
mkdir -p "${WORKSPACE_ROOT:-/data/workspaces}" "${LOG_DIR:-/data/logs}"

echo "Starting Coding Agent on ${AGENT_HOST:-0.0.0.0}:${AGENT_PORT:-8100}..."
exec python agent.py
