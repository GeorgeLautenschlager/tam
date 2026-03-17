#!/usr/bin/env bash
# tam.sh — Tam's cron wrapper
# Invokes Claude Code in headless mode, pointing at the config files.
# 
# Usage:
#   ./tam.sh              # normal cron run
#   ./tam.sh "check my calendar for tomorrow"   # ad-hoc prompt
#
# Cron example (every 30 minutes during waking hours):
#   */30 8-21 * * * /home/george/tam/tam.sh >> /home/george/tam/logs/cron.log 2>&1

set -euo pipefail

# ── Config ──────────────────────────────────────────────────────────────────
TAM_HOME="${TAM_HOME:-$HOME/tam}"
TAM_LOGS="${TAM_HOME}/logs"
TAM_MAX_TURNS="${TAM_MAX_TURNS:-6}"
TAM_TIMEOUT="${TAM_TIMEOUT:-300}"  # 5 minutes in seconds

# ── Load .env ──────────────────────────────────────────────────────────────
ENV_FILE="${TAM_HOME}/.env"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# ── Setup ───────────────────────────────────────────────────────────────────
mkdir -p "$TAM_LOGS"

TIMESTAMP=$(date '+%Y-%m-%d_%H%M')
RUN_LOG="${TAM_LOGS}/run_${TIMESTAMP}.log"

# ── Build the prompt ────────────────────────────────────────────────────────
# If an argument is passed, it's an ad-hoc request. Otherwise, standard wake-up.
# Prompts are deliberately prescriptive to prevent Tam from exploring/wasting turns.
if [[ $# -gt 0 ]]; then
    PROMPT="You are Tam. Do these steps in order:
1. Read MEMORY.md
2. Read STATE.md
3. Address this request from George: $*
4. Update STATE.md with what you did
Only read the files listed above. Do not explore the filesystem. Do not search for tools."
else
    PROMPT="You are Tam. Do these steps in order:
1. Read MEMORY.md
2. Read STATE.md
3. Decide if anything needs George's attention (check Pending items, Ongoing Threads)
4. Update STATE.md with the current timestamp and what you found
If George needs to know something, start your final response with NOTIFY: and a short message.
If nothing notable, just update STATE.md and respond with a one-line summary.
Only read the files listed above. Do not explore the filesystem. Do not search for tools."
fi

# ── Build the system prompt from SOUL.md ────────────────────────────────────
# Injecting SOUL.md as a system prompt means it's always in context without
# using a Read tool call (saves a turn and tokens on every run).
# Pass file contents directly — no compound string building, no quoting headaches.
SYSTEM_PROMPT=$(cat "${TAM_HOME}/SOUL.md")

# ── Allowed tools ───────────────────────────────────────────────────────────
# Start conservative. Read/Write for state files is all cron needs.
ALLOWED_TOOLS="Read,Write"

# ── Run ─────────────────────────────────────────────────────────────────────
echo "=== Tam run: ${TIMESTAMP} ===" | tee "$RUN_LOG"
echo "Prompt: ${PROMPT}" >> "$RUN_LOG"

cd "$TAM_HOME"

RESULT=$(timeout "$TAM_TIMEOUT" claude -p "$PROMPT" \
    --append-system-prompt "$SYSTEM_PROMPT" \
    --allowedTools "$ALLOWED_TOOLS" \
    --dangerously-skip-permissions \
    --max-turns "$TAM_MAX_TURNS" \
    --output-format json \
    2>>"$RUN_LOG") || {
    echo "ERROR: Claude Code exited with code $?" | tee -a "$RUN_LOG"
    exit 1
}

# ── Parse output ────────────────────────────────────────────────────────────
TEXT_RESULT=$(echo "$RESULT" | jq -r '.result // "No result returned"')
COST=$(echo "$RESULT" | jq -r '.total_cost_usd // "unknown"')
SESSION_ID=$(echo "$RESULT" | jq -r '.session_id // "unknown"')

echo "Session: ${SESSION_ID}" >> "$RUN_LOG"
echo "Cost: \$${COST} USD" >> "$RUN_LOG"
echo "Result:" >> "$RUN_LOG"
echo "$TEXT_RESULT" >> "$RUN_LOG"

# ── Usage ledger ────────────────────────────────────────────────────────────
echo "${TIMESTAMP} | cron | cost=${COST} | session=${SESSION_ID}" >> "${TAM_HOME}/USAGE.log"

# ── Notifications ───────────────────────────────────────────────────────────
# Tam signals via a NOTIFY: line. We send it to Discord via webhook.
NOTIFICATION=$(echo "$TEXT_RESULT" | grep -i "^NOTIFY:" | sed 's/^NOTIFY:\s*//' || true)

if [[ -n "$NOTIFICATION" ]]; then
    echo "Sending notification: ${NOTIFICATION}" >> "$RUN_LOG"

    if [[ -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
        curl -s -H "Content-Type: application/json" \
            -d "{\"content\": \"**Tam:** ${NOTIFICATION}\"}" \
            "$DISCORD_WEBHOOK_URL" >> "$RUN_LOG" 2>&1
    else
        echo "(No webhook configured — notification not sent)" >> "$RUN_LOG"
    fi
fi

# ── Cleanup ─────────────────────────────────────────────────────────────────
# Keep last 100 run logs, prune the rest
find "$TAM_LOGS" -name "run_*.log" -type f | sort -r | tail -n +101 | xargs -r rm

echo "=== Tam run complete ===" >> "$RUN_LOG"
