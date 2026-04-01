#!/usr/bin/env bash
# Hook: pre-reflect (UserPromptSubmit)
# Detects /reflect in the prompt and injects STATE.md + task queue as context
# so the reflect skill has current system state without manual copy-paste.
set -euo pipefail

# Read stdin in bash (heredoc syntax would consume it)
export HOOK_INPUT
HOOK_INPUT=$(cat)

python3 /home/aldric/tam/.claude/hooks/pre-reflect.py
