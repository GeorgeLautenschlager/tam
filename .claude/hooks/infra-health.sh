#!/usr/bin/env bash
# Hook: infra-health (SessionStart)
# Injects systemd service health so Tam knows if Discord or vault-indexer are down.
set -euo pipefail

DBUS="unix:path=/run/user/$(id -u)/bus"

svc_status() {
  local name="$1"
  local state
  state=$(DBUS_SESSION_BUS_ADDRESS="$DBUS" systemctl --user is-active "$name" 2>/dev/null || echo "unknown")
  echo "$name: $state"
}

HEALTH="Infrastructure status:
$(svc_status tam-discord.service)
$(svc_status tam-indexer.service)"

# Escape for JSON
HEALTH_ESCAPED=$(python3 -c "
import json, sys
print(json.dumps(sys.stdin.read()))
" <<< "$HEALTH")

cat <<EOF
{
  "continue": true,
  "suppressOutput": true,
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": ${HEALTH_ESCAPED}
  }
}
EOF
