#!/usr/bin/env bash
# Hook: intent-detection (UserPromptSubmit)
# Detects project names in the prompt and auto-injects relevant context.
# Projects: CVI Aid, Feudal Carriers, Project Ender, Violet.
set -euo pipefail

export HOOK_INPUT
HOOK_INPUT=$(cat)

python3 /home/aldric/tam/.claude/hooks/intent-detection.py
