#!/usr/bin/env bash
# Hook: inject-time (UserPromptSubmit)
# Injects current date/time so Tam always knows when it is.
set -euo pipefail

NOW=$(date '+%Y-%m-%d %H:%M %Z (%A)')

cat <<EOF
{
  "continue": true,
  "suppressOutput": true,
  "systemMessage": "Current time: ${NOW}"
}
EOF
