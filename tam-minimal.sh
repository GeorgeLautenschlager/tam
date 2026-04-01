#!/usr/bin/env bash
# tam.sh — minimal version to debug hanging issue

cd "$HOME/tam"

timeout 300 claude -p "You are Tam. Read MEMORY.md and STATE.md, do what you think is appropriate, and update STATE.md when done. Do not explore outside ~/tam." \
    --append-system-prompt "$(cat SOUL.md)" \
    --allowedTools "Read,Write" \
    --dangerously-skip-permissions \
    --max-turns 15 \
    --output-format json < /dev/null
