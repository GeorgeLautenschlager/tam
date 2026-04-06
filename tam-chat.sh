#!/usr/bin/env bash
# tam-chat.sh — Interactive conversation with Tam
#
# Usage:
#   ./tam-chat.sh                      # new conversation (Opus)
#   ./tam-chat.sh --resume             # continue last session
#   ./tam-chat.sh --resume <id>        # continue specific session
#   ./tam-chat.sh --deliberate         # Opus — deep reasoning (default for chat)
#   ./tam-chat.sh --flow               # Sonnet — practical execution
#   ./tam-chat.sh --reflex             # Haiku — fast, lightweight

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

# ── Model selection ─────────────────────────────────────────────────────────
# Default: Opus for conversations. Override via --model flag or TAM_MODEL env var.
resolve_model() {
    case "$1" in
        opus)   echo "claude-opus-4-6" ;;
        sonnet) echo "claude-sonnet-4-6" ;;
        haiku)  echo "claude-haiku-4-5-20251001" ;;
        *)      echo "$1" ;;
    esac
}

TAM_MODEL="${TAM_MODEL:-opus}"

SYSTEM_PROMPT=$(cat "${TAM_HOME}/docs/SOUL.md")

# ── Parse args ──────────────────────────────────────────────────────────────
RESUME_ARGS=""
while [[ $# -gt 0 ]]; do
    case "${1:-}" in
        --model)
            TAM_MODEL="${2:?--model requires a value}"
            shift 2
            ;;
        --deliberate) TAM_MODEL="opus";   shift ;;
        --flow)       TAM_MODEL="sonnet"; shift ;;
        --reflex)     TAM_MODEL="haiku";  shift ;;
        --resume)
            if [[ -n "${2:-}" && "${2:-}" != --* ]]; then
                RESUME_ARGS="--resume ${2}"
                shift 2
            elif [[ -f "$LAST_SESSION_FILE" ]]; then
                LAST_ID=$(cat "$LAST_SESSION_FILE")
                RESUME_ARGS="--resume ${LAST_ID}"
                echo "Resuming session: ${LAST_ID}"
                shift
            else
                echo "No previous session found. Starting fresh."
                shift
            fi
            ;;
        *)
            break
            ;;
    esac
done

MODEL=$(resolve_model "$TAM_MODEL")

# ── Launch ──────────────────────────────────────────────────────────────────
cd "$TAM_HOME"

echo "Starting Tam... (${MODEL})"
echo "──────────────────────────────────────"

claude $RESUME_ARGS \
    --model "$MODEL" \
    --append-system-prompt "$SYSTEM_PROMPT

You are in interactive mode. George is talking to you directly.

Start by reading MEMORY.md and STATE.md to orient yourself, then greet George.
Keep it casual — this is a conversation, not a cron run.
If George asks you to do something that changes ongoing context, update the
relevant file (MEMORY.md, STATE.md) before the conversation ends."
