#!/usr/bin/env bash
# tam.sh — Tam's cron wrapper
# Invokes Claude Code in headless mode, pointing at the config files.
# 
# Usage:
#   ./tam.sh              # normal cron run
#   ./tam.sh "check my calendar for tomorrow"   # ad-hoc prompt
#
# Cron example (heartbeat every 15 min, schedule.json controls actual runs):
#   */15 * * * * /home/aldric/tam/tam.sh >> /home/aldric/tam/logs/cron.log 2>&1

set -euo pipefail

# ── Config ──────────────────────────────────────────────────────────────────
TAM_HOME="${TAM_HOME:-$HOME/tam}"
TAM_LOGS="${TAM_HOME}/logs"
TAM_MAX_TURNS="${TAM_MAX_TURNS:-50}"
TAM_TIMEOUT="${TAM_TIMEOUT:-600}"  # 10 minutes in seconds
LOCK_FILE="${TAM_HOME}/.tam.lock"
SCHEDULE_FILE="${TAM_HOME}/schedule.json"
PAUSE_FILE="${TAM_HOME}/.tam-pause"
SKIP_GATES=false

# ── Parse flags ───────────────────────────────────────────────────────────────
# --force skips pause, schedule, and budget checks
if [[ "${1:-}" == "--force" ]]; then
    SKIP_GATES=true
    shift
fi

# ── Pause check ───────────────────────────────────────────────────────────────
# Emergency stop: touch .tam-pause to halt all cron runs. Only George removes it.
if [[ "$SKIP_GATES" != "true" && -f "$PAUSE_FILE" ]]; then
    exit 0
fi

# ── Schedule gate ─────────────────────────────────────────────────────────────
# Reads schedule.json to decide if it's time to run. The heartbeat cron fires
# every 15 minutes; this gate is what actually controls the schedule.
if [[ "$SKIP_GATES" != "true" && -f "$SCHEDULE_FILE" ]]; then
    ENABLED=$(jq -r '.enabled // true' "$SCHEDULE_FILE" 2>/dev/null || echo "true")
    if [[ "$ENABLED" != "true" ]]; then
        exit 0
    fi

    NEXT_RUN=$(jq -r '.next_run_after // ""' "$SCHEDULE_FILE" 2>/dev/null || echo "")
    if [[ -n "$NEXT_RUN" ]]; then
        NEXT_EPOCH=$(date -d "$NEXT_RUN" +%s 2>/dev/null || echo 0)
        NOW_EPOCH=$(date +%s)
        if [[ $NOW_EPOCH -lt $NEXT_EPOCH ]]; then
            exit 0  # Not time yet — silent skip
        fi
    fi

    # Quiet hours check (e.g. 22-07 = no runs except overnight)
    CURRENT_HOUR=$(date +%-H)
    QUIET_START=$(jq -r '.quiet_hours.start // 22' "$SCHEDULE_FILE" 2>/dev/null || echo 22)
    QUIET_END=$(jq -r '.quiet_hours.end // 7' "$SCHEDULE_FILE" 2>/dev/null || echo 7)
    OVERNIGHT=$(jq -r '.overnight_run // ""' "$SCHEDULE_FILE" 2>/dev/null || echo "")
    OVERNIGHT_HOUR=""
    if [[ -n "$OVERNIGHT" ]]; then
        OVERNIGHT_HOUR=$(echo "$OVERNIGHT" | cut -d: -f1 | sed 's/^0//')
    fi

    IN_QUIET=false
    if [[ $QUIET_START -gt $QUIET_END ]]; then
        # Wraps midnight: e.g. 22-07
        if [[ $CURRENT_HOUR -ge $QUIET_START || $CURRENT_HOUR -lt $QUIET_END ]]; then
            IN_QUIET=true
        fi
    else
        if [[ $CURRENT_HOUR -ge $QUIET_START && $CURRENT_HOUR -lt $QUIET_END ]]; then
            IN_QUIET=true
        fi
    fi

    # Allow overnight run exception
    if [[ "$IN_QUIET" == "true" ]]; then
        if [[ -n "$OVERNIGHT_HOUR" && "$CURRENT_HOUR" == "$OVERNIGHT_HOUR" ]]; then
            IN_QUIET=false  # Allow the overnight run
        fi
    fi

    if [[ "$IN_QUIET" == "true" ]]; then
        exit 0  # Quiet hours — silent skip
    fi
fi

# ── Lock — skip if already running ──────────────────────────────────────────
if [[ -f "$LOCK_FILE" ]]; then
    LOCK_PID=$(cat "$LOCK_FILE")
    if kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d_%H%M') Tam already running (PID ${LOCK_PID}). Skipping." \
            >> "${TAM_HOME}/logs/cron.log"
        exit 0
    else
        echo "$(date '+%Y-%m-%d_%H%M') Stale lock (PID ${LOCK_PID}). Cleaning up." \
            >> "${TAM_HOME}/logs/cron.log"
        rm -f "$LOCK_FILE"
    fi
fi
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

# ── Load .env ──────────────────────────────────────────────────────────────
ENV_FILE="${TAM_HOME}/.env"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# ── Model selection ─────────────────────────────────────────────────────────
# Default: Sonnet. Override via --model flag or TAM_MODEL env var.
# Shorthands: opus, sonnet, haiku
resolve_model() {
    case "$1" in
        opus)   echo "claude-opus-4-6" ;;
        sonnet) echo "claude-sonnet-4-6" ;;
        haiku)  echo "claude-haiku-4-5-20251001" ;;
        *)      echo "$1" ;;  # pass full model IDs through
    esac
}

TAM_MODEL="${TAM_MODEL:-sonnet}"

# Parse --model flag (must come before any prompt args)
if [[ "${1:-}" == "--model" ]]; then
    TAM_MODEL="${2:?--model requires a value}"
    shift 2
fi

MODEL=$(resolve_model "$TAM_MODEL")

# ── Budget gate ───────────────────────────────────────────────────────────────
# Check budget before doing anything expensive. Fail-safe: if checker errors, skip run.
BUDGET_CONTEXT=""
if [[ "$SKIP_GATES" != "true" ]]; then
    BUDGET_JSON=$(python3 "${TAM_HOME}/tam-budget.py" --source cron 2>/dev/null) || BUDGET_EXIT=$?
    BUDGET_EXIT=${BUDGET_EXIT:-0}

    if [[ $BUDGET_EXIT -eq 2 ]]; then
        # Budget checker errored — fail safe, don't run
        echo "$(date '+%Y-%m-%d_%H%M') Budget checker error. Skipping run." >> "${TAM_HOME}/logs/cron.log"
        exit 0
    elif [[ $BUDGET_EXIT -eq 1 ]]; then
        # Over budget
        REASON=$(echo "$BUDGET_JSON" | jq -r '.reason // "unknown"' 2>/dev/null)
        DAILY_PCT=$(echo "$BUDGET_JSON" | jq -r '.daily_pct // "?"' 2>/dev/null)
        WEEKLY_PCT=$(echo "$BUDGET_JSON" | jq -r '.weekly_pct // "?"' 2>/dev/null)
        echo "$(date '+%Y-%m-%d_%H%M') | cron | budget_blocked | ${REASON} (daily=${DAILY_PCT}%, weekly=${WEEKLY_PCT}%)" \
            >> "${TAM_HOME}/USAGE.log"
        echo "$(date '+%Y-%m-%d_%H%M') Budget blocked (${REASON}). Skipping run." >> "${TAM_HOME}/logs/cron.log"
        exit 0
    fi

    # Budget OK — save context for injection into prompt
    BUDGET_CONTEXT="$BUDGET_JSON"
fi

# ── Setup ───────────────────────────────────────────────────────────────────
mkdir -p "$TAM_LOGS"

TIMESTAMP=$(date '+%Y-%m-%d_%H%M')
TODAY=$(date '+%Y-%m-%d')
RUN_LOG="${TAM_LOGS}/run_${TIMESTAMP}.log"

# ── Session resumption ─────────────────────────────────────────────────────
# Resume the same session throughout the day. New day = fresh session.
CRON_SESSION_FILE="${TAM_HOME}/.cron-session"
RESUME_SESSION=""

if [[ -f "$CRON_SESSION_FILE" ]]; then
    SESSION_DATE=$(jq -r '.date // ""' "$CRON_SESSION_FILE" 2>/dev/null)
    PREV_SESSION=$(jq -r '.session_id // ""' "$CRON_SESSION_FILE" 2>/dev/null)
    if [[ "$SESSION_DATE" == "$TODAY" && -n "$PREV_SESSION" ]]; then
        RESUME_SESSION="$PREV_SESSION"
        echo "Resuming session ${RESUME_SESSION} from today" >> "$RUN_LOG"
    else
        echo "New day — starting fresh session (previous: ${SESSION_DATE})" >> "$RUN_LOG"
    fi
fi

# ── Build the prompt ────────────────────────────────────────────────────────
# If an argument is passed, it's an ad-hoc request. Otherwise, standard wake-up.
if [[ $# -gt 0 ]]; then
    PROMPT="You are Tam. Read BOOT.md and follow the wake-up sequence, \
then address this request from George: $*
If something needs George's attention, start your final response with NOTIFY: and a short message."
else
    PROMPT="You are Tam. Read BOOT.md and follow the wake-up sequence.
Check the Task Queue in /home/aldric/vaults/tam/State/Task Queue.md — if there are queued tasks, work on one.
If the queue is empty, follow the Autonomous Activity guidelines in BOOT.md.
If something needs George's attention, start your final response with NOTIFY: and a short message.
IMPORTANT: Always end your run with a text summary of what you did. Your final message MUST be text, not a tool call — the cron wrapper parses it for reporting."
fi

# ── Build the system prompt from SOUL.md ────────────────────────────────────
# Injecting SOUL.md as a system prompt means it's always in context without
# using a Read tool call (saves a turn and tokens on every run).
# Pass file contents directly — no compound string building, no quoting headaches.
SYSTEM_PROMPT=$(cat "${TAM_HOME}/SOUL.md")

# ── Allowed tools ───────────────────────────────────────────────────────────
# Read/Write for state files, Bash for system commands and task work.
ALLOWED_TOOLS="Read,Write,Edit,Glob,Grep,Bash"

# ── GitHub activity ─────────────────────────────────────────────────────────
GITHUB_FINDINGS=$(python3 "${TAM_HOME}/tam-github.py" 2>>"$RUN_LOG" || echo "")
if [[ -n "$GITHUB_FINDINGS" && "$GITHUB_FINDINGS" != "NONE" ]]; then
    PROMPT="${PROMPT}

GitHub activity on cvi-aid since last run:
${GITHUB_FINDINGS}"
fi

# ── Budget context injection ──────────────────────────────────────────────────
if [[ -n "$BUDGET_CONTEXT" ]]; then
    PROMPT="${PROMPT}

Budget context for this run (from tam-budget.py):
${BUDGET_CONTEXT}
Use this to calibrate your behavior — see BOOT.md section 2b for guidelines."
fi

# ── Run ─────────────────────────────────────────────────────────────────────
echo "=== Tam run: ${TIMESTAMP} ===" | tee "$RUN_LOG"
echo "Prompt: ${PROMPT}" >> "$RUN_LOG"

cd "$TAM_HOME"

echo "Model: ${MODEL}" >> "$RUN_LOG"

EXIT_CODE=0
if [[ -n "$RESUME_SESSION" ]]; then
    echo "Mode: resume (session ${RESUME_SESSION})" >> "$RUN_LOG"
    RESULT=$(timeout "$TAM_TIMEOUT" claude -p "$PROMPT" \
        --model "$MODEL" \
        --resume "$RESUME_SESSION" \
        --allowedTools "$ALLOWED_TOOLS" \
        --dangerously-skip-permissions \
        --max-turns "$TAM_MAX_TURNS" \
        --output-format json \
        < /dev/null 2>>"$RUN_LOG") || EXIT_CODE=$?
else
    echo "Mode: new session" >> "$RUN_LOG"
    RESULT=$(timeout "$TAM_TIMEOUT" claude -p "$PROMPT" \
        --model "$MODEL" \
        --append-system-prompt "$SYSTEM_PROMPT" \
        --allowedTools "$ALLOWED_TOOLS" \
        --dangerously-skip-permissions \
        --max-turns "$TAM_MAX_TURNS" \
        --output-format json \
        < /dev/null 2>>"$RUN_LOG") || EXIT_CODE=$?
fi

# ── Parse output ────────────────────────────────────────────────────────────
# Debug: dump raw JSON structure to run log for diagnosing empty results
echo "JSON keys: $(echo "$RESULT" | jq -r 'keys | join(", ")' 2>/dev/null)" >> "$RUN_LOG"
echo "stop_reason: $(echo "$RESULT" | jq -r '.stop_reason // "unknown"' 2>/dev/null)" >> "$RUN_LOG"
echo "num_turns: $(echo "$RESULT" | jq -r '.num_turns // "unknown"' 2>/dev/null)" >> "$RUN_LOG"
echo "Result field type: $(echo "$RESULT" | jq -r '.result | type' 2>/dev/null)" >> "$RUN_LOG"
echo "Raw result field (first 500 chars): $(echo "$RESULT" | jq -r '.result' 2>/dev/null | head -c 500)" >> "$RUN_LOG"

STOP_REASON=$(echo "$RESULT" | jq -r '.stop_reason // "unknown"' 2>/dev/null)
NUM_TURNS=$(echo "$RESULT" | jq -r '.num_turns // "unknown"' 2>/dev/null)
TEXT_RESULT=$(echo "$RESULT" | jq -r 'if .result == null then "" else .result end' 2>/dev/null)

# If result is empty/null (e.g. max turns hit), build a fallback message
if [[ -z "$TEXT_RESULT" ]]; then
    TEXT_RESULT="[No text result — stop_reason=${STOP_REASON}, turns=${NUM_TURNS}/${TAM_MAX_TURNS}. Run was likely productive but exhausted all turns on tool calls.]"
fi
COST=$(echo "$RESULT" | jq -r '.total_cost_usd // "unknown"' 2>/dev/null || echo "unknown")
SESSION_ID=$(echo "$RESULT" | jq -r '.session_id // "unknown"' 2>/dev/null || echo "unknown")

# ── Rate limit detection ──────────────────────────────────────────────────
if [[ "$COST" == "0" ]] && echo "$TEXT_RESULT" | grep -q "You've hit your limit"; then
    echo "Rate limited: ${TEXT_RESULT}" >> "$RUN_LOG"
    echo "${TIMESTAMP} | cron | rate_limited | ${TEXT_RESULT}" >> "${TAM_HOME}/USAGE.log"

    # Parse the reset time and suppress runs until then.
    # Rate limit message format: "resets 12pm (America/Toronto)"
    RESET_HINT=$(echo "$TEXT_RESULT" | grep -oP 'resets \K[0-9]+[ap]m' || echo "")
    if [[ -n "$RESET_HINT" && -f "$SCHEDULE_FILE" ]]; then
        # Convert "12pm" -> today's date at that hour
        RESET_TIME=$(date -d "today $RESET_HINT" --iso-8601=seconds 2>/dev/null || "")
        if [[ -n "$RESET_TIME" ]]; then
            NOW_ISO=$(date --iso-8601=seconds)
            jq --arg next "$RESET_TIME" --arg now "$NOW_ISO" \
                '.next_run_after = $next | .modified_by = "tam.sh (rate-limited)" | .modified_at = $now | .reason = "Rate limited — suppressing until reset"' \
                "$SCHEDULE_FILE" > "${SCHEDULE_FILE}.tmp" && mv "${SCHEDULE_FILE}.tmp" "$SCHEDULE_FILE"
            echo "Schedule updated: suppressing runs until rate limit resets at ${RESET_TIME}" >> "$RUN_LOG"
        fi
    fi

    # Clear the cron session so the next successful run starts fresh.
    # Without this, each rate-limited resume appends the cron prompt to the
    # session history, causing prompt duplication when the limit clears.
    if [[ -f "$CRON_SESSION_FILE" ]]; then
        rm -f "$CRON_SESSION_FILE"
        echo "Cleared cron session (prevents stale prompt accumulation on resume)" >> "$RUN_LOG"
    fi

    echo "=== Tam run complete (rate limited) ===" >> "$RUN_LOG"
    exit 0
fi

# ── Handle other errors ──────────────────────────────────────────────────
if [[ $EXIT_CODE -ne 0 ]]; then
    echo "ERROR: Claude Code exited with code ${EXIT_CODE}" | tee -a "$RUN_LOG"
    exit 1
fi

echo "Session: ${SESSION_ID}" >> "$RUN_LOG"
echo "Cost: \$${COST} USD" >> "$RUN_LOG"
echo "Result:" >> "$RUN_LOG"
echo "$TEXT_RESULT" >> "$RUN_LOG"

# ── Persist session for resumption ──────────────────────────────────────────
if [[ "$SESSION_ID" != "unknown" && -n "$SESSION_ID" ]]; then
    echo "{\"date\": \"${TODAY}\", \"session_id\": \"${SESSION_ID}\"}" > "$CRON_SESSION_FILE"
    echo "Session saved for resumption: ${SESSION_ID}" >> "$RUN_LOG"
fi

# ── Usage ledger ────────────────────────────────────────────────────────────
echo "${TIMESTAMP} | cron | cost=${COST} | session=${SESSION_ID}" >> "${TAM_HOME}/USAGE.log"

# ── Post-run schedule update ──────────────────────────────────────────────────
# Fallback: if Tam didn't update schedule.json during the run, bump next_run_after
# based on the configured interval. This ensures the heartbeat cron doesn't
# immediately re-trigger.
if [[ -f "$SCHEDULE_FILE" ]]; then
    RUN_START_EPOCH=$(date -d "${TIMESTAMP}" '+%s' 2>/dev/null || echo 0)
    MODIFIED_AT=$(jq -r '.modified_at // ""' "$SCHEDULE_FILE" 2>/dev/null || echo "")
    MODIFIED_EPOCH=0
    if [[ -n "$MODIFIED_AT" ]]; then
        MODIFIED_EPOCH=$(date -d "$MODIFIED_AT" '+%s' 2>/dev/null || echo 0)
    fi

    # Only update if Tam didn't already modify schedule.json during this run
    if [[ $MODIFIED_EPOCH -lt $RUN_START_EPOCH ]]; then
        INTERVAL=$(jq -r '.interval_minutes // 60' "$SCHEDULE_FILE" 2>/dev/null || echo 60)
        NEXT=$(date -d "+${INTERVAL} minutes" --iso-8601=seconds)
        NOW_ISO=$(date --iso-8601=seconds)
        jq --arg next "$NEXT" --arg now "$NOW_ISO" \
            '.next_run_after = $next | .modified_by = "tam.sh" | .modified_at = $now | .reason = "Post-run fallback — Tam did not update schedule during run"' \
            "$SCHEDULE_FILE" > "${SCHEDULE_FILE}.tmp" && mv "${SCHEDULE_FILE}.tmp" "$SCHEDULE_FILE"
        echo "Schedule updated: next run after ${NEXT} (interval=${INTERVAL}m)" >> "$RUN_LOG"
    else
        echo "Schedule already updated by Tam during run — skipping fallback" >> "$RUN_LOG"
    fi
fi

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

# ── Cron report ────────────────────────────────────────────────────────────
# Post a summary of every cron run to the cron-reports channel.
CRON_REPORT_CHANNEL="${DISCORD_CRON_REPORT_CHANNEL:-}"

if [[ -n "$CRON_REPORT_CHANNEL" && -n "${DISCORD_TOKEN:-}" ]]; then
    # Truncate to Discord's 2000 char limit, leaving room for the header
    REPORT_HEADER="**Cron run ${TIMESTAMP}** | \$${COST} | ${MODEL}"
    MAX_BODY=$((1900 - ${#REPORT_HEADER}))
    REPORT_BODY=$(echo "$TEXT_RESULT" | head -c "$MAX_BODY")
    REPORT_CONTENT="${REPORT_HEADER}\n${REPORT_BODY}"

    # Escape for JSON
    REPORT_JSON=$(printf '%s' "$REPORT_CONTENT" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')

    curl -s -X POST \
        "https://discord.com/api/v10/channels/${CRON_REPORT_CHANNEL}/messages" \
        -H "Authorization: Bot ${DISCORD_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"content\": ${REPORT_JSON}}" >> "$RUN_LOG" 2>&1
    echo "Cron report posted to channel ${CRON_REPORT_CHANNEL}" >> "$RUN_LOG"
fi

# ── Cleanup ─────────────────────────────────────────────────────────────────
# Keep last 100 run logs, prune the rest
find "$TAM_LOGS" -name "run_*.log" -type f | sort -r | tail -n +101 | xargs -r rm

echo "=== Tam run complete ===" >> "$RUN_LOG"
