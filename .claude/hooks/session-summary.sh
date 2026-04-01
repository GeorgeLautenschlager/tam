#!/usr/bin/env bash
# Hook: session-summary (Stop)
# Appends a one-line session summary to the session log for continuity.
set -euo pipefail

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id','unknown'))" 2>/dev/null || echo "unknown")
TRANSCRIPT=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null || echo "")

LOG_DIR="/home/aldric/tam/.claude/hooks"
LOG_FILE="${LOG_DIR}/recent-sessions.log"

# Keep only last 10 entries to stay concise
if [ -f "$LOG_FILE" ] && [ "$(wc -l < "$LOG_FILE")" -ge 10 ]; then
  tail -5 "$LOG_FILE" > "${LOG_FILE}.tmp"
  mv "${LOG_FILE}.tmp" "$LOG_FILE"
fi

TIMESTAMP=$(date '+%Y-%m-%d %H:%M')

# Extract the last assistant text response as the session summary.
# The transcript uses type:"user" with content in msg['message']['content'],
# and type:"assistant" with content in msg['message']['content'].
# Cron first-user-message is always the wake-up prompt — skip it.
SUMMARY=""
if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ]; then
  SUMMARY=$(python3 -c "
import json, sys

CRON_SKIP = 'You are Tam. Read BOOT.md'

def extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'text':
                return block.get('text', '')
    return ''

try:
    with open('$TRANSCRIPT', 'r') as f:
        lines = f.readlines()

    last_assistant_text = ''
    for line in lines:
        try:
            msg = json.loads(line)
            if msg.get('type') == 'assistant':
                inner = msg.get('message', {})
                content = inner.get('content', '')
                text = extract_text(content)
                if text and len(text) > 20:
                    last_assistant_text = text
        except (json.JSONDecodeError, KeyError):
            continue

    if last_assistant_text:
        # Take first 120 chars, collapse whitespace
        summary = ' '.join(last_assistant_text.split())[:120]
        print(summary)
    else:
        print('(no topic extracted)')
except Exception as e:
    print(f'(error: {e})')
" 2>/dev/null || echo "(transcript parse failed)")
fi

echo "${TIMESTAMP} | ${SESSION_ID:0:8} | ${SUMMARY}" >> "$LOG_FILE"

cat <<EOF
{
  "continue": true,
  "suppressOutput": true
}
EOF
