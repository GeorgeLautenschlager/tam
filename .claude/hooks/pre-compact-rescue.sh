#!/usr/bin/env bash
# Hook: pre-compact-rescue (PreCompact)
# Injects a system message prompting Claude to persist any unsaved
# decisions, preferences, or context before compaction erases them.
set -euo pipefail

MEMORY_INDEX="/home/aldric/.claude/projects/-home-aldric-tam/memory/MEMORY.md"

python3 << 'PYEOF'
import json

try:
    with open("/home/aldric/.claude/projects/-home-aldric-tam/memory/MEMORY.md") as f:
        memory_index = f.read().strip()
except FileNotFoundError:
    memory_index = "(no memory index found)"

msg = f"""CONTEXT COMPACTION IMMINENT — memory rescue check.

Review this conversation for any of the following that have NOT yet been persisted:
1. Decisions George made (technical, architectural, process)
2. Preferences or corrections George expressed (feedback memories)
3. New project context or status changes
4. External references George mentioned (URLs, systems, people)

Already persisted in MEMORY.md:
{memory_index}

Instructions:
- For each unpersisted item, save it using the appropriate tool (Write for auto-memory files, or remember for tam-memory)
- Update STATE.md if any hot state changed
- If everything is already captured, do nothing
- Be concise. This is a rescue operation, not a reflection."""

print(json.dumps({
    "continue": True,
    "suppressOutput": True,
    "systemMessage": msg
}))
PYEOF
