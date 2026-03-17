#!/usr/bin/env bash
# tam-chat.sh — Interactive conversation with Tam
#
# Usage:
#   ./tam-chat.sh                  # new conversation
#   ./tam-chat.sh --resume         # continue last session
#   ./tam-chat.sh --resume <id>    # continue specific session

set -euo pipefail

TAM_HOME="${TAM_HOME:-$HOME/tam}"
LAST_SESSION_FILE="${TAM_HOME}/.last_session_id"

# Load .env if present
ENV_FILE="${TAM_HOME}/.env"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

SYSTEM_PROMPT=$(cat "${TAM_HOME}/SOUL.md")

# ── Handle resume ───────────────────────────────────────────────────────────
RESUME_ARGS=""
if [[ "${1:-}" == "--resume" ]]; then
    if [[ -n "${2:-}" ]]; then
        # Explicit session ID
        RESUME_ARGS="--resume ${2}"
    elif [[ -f "$LAST_SESSION_FILE" ]]; then
        # Resume last session
        LAST_ID=$(cat "$LAST_SESSION_FILE")
        RESUME_ARGS="--resume ${LAST_ID}"
        echo "Resuming session: ${LAST_ID}"
    else
        echo "No previous session found. Starting fresh."
    fi
fi

# ── Launch ──────────────────────────────────────────────────────────────────
cd "$TAM_HOME"

echo "Starting Tam..."
echo "──────────────────────────────────────"

claude $RESUME_ARGS \
    --append-system-prompt "$SYSTEM_PROMPT

You are in interactive mode. George is talking to you directly.

Start by reading MEMORY.md and STATE.md to orient yourself, then greet George.
Keep it casual — this is a conversation, not a cron run.
If George asks you to do something that changes ongoing context, update the
relevant file (MEMORY.md, STATE.md) before the conversation ends."
