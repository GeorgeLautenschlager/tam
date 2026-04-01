#!/usr/bin/env bash
# Hook: recent-context (SessionStart)
# Injects recent session summaries so Tam has continuity across sessions.
set -euo pipefail

LOG_FILE="/home/aldric/tam/.claude/hooks/recent-sessions.log"
NOW=$(date '+%Y-%m-%d %H:%M %Z (%A)')

CONTEXT="Current time: ${NOW}"

if [ -f "$LOG_FILE" ] && [ -s "$LOG_FILE" ]; then
  RECENT=$(tail -5 "$LOG_FILE")
  CONTEXT="${CONTEXT}

Recent sessions (most recent last):
${RECENT}"
fi

# Escape for JSON
CONTEXT_ESCAPED=$(python3 -c "
import json, sys
print(json.dumps(sys.stdin.read()))
" <<< "$CONTEXT")

cat <<EOF
{
  "continue": true,
  "suppressOutput": true,
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": ${CONTEXT_ESCAPED}
  }
}
EOF
