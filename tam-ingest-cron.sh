#!/usr/bin/env bash
# tam-ingest-cron.sh — Nightly ingestion of new sessions + Discord logs
# Cron: 0 3 * * * /home/aldric/tam/tam-ingest-cron.sh >> /home/aldric/tam/logs/ingest-cron.log 2>&1

set -euo pipefail

TAM_HOME="${TAM_HOME:-$HOME/tam}"
VENV="${TAM_HOME}/.venv/bin/python"

echo "=== Ingest run: $(date '+%Y-%m-%d %H:%M') ==="

# Ingest new Claude Code sessions
"$VENV" "${TAM_HOME}/tam-ingest.py" 2>&1

# Re-ingest Discord logs (deletes old entry first, re-processes full log)
sqlite3 "${TAM_HOME}/tam_memory.db" "DELETE FROM sessions_ingested WHERE session_id = 'discord_log';" 2>/dev/null || true
"$VENV" "${TAM_HOME}/tam-ingest.py" --discord 2>&1

echo "=== Ingest complete ==="
