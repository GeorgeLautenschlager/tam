#!/usr/bin/env python3
"""
Hook: pre-reflect (UserPromptSubmit)
Detects /reflect in the prompt and injects STATE.md + task queue as context.
"""
import json
import sys
import os

hook_input = os.environ.get("HOOK_INPUT", "")

try:
    data = json.loads(hook_input) if hook_input.strip() else {}
except Exception:
    data = {}

# Extract prompt text
prompt = data.get("prompt", "")
if not isinstance(prompt, str):
    try:
        prompt = " ".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in prompt
        )
    except Exception:
        prompt = str(prompt)

if "/reflect" not in prompt.lower():
    print(json.dumps({"continue": True}))
    sys.exit(0)

# /reflect detected — gather context
STATE_FILE = "/home/aldric/tam/STATE.md"
TASK_QUEUE = "/home/aldric/vaults/tam/State/Task Queue.md"
PROJECTS = "/home/aldric/vaults/tam/State/Projects.md"

sections = []

def read_file(path, label, max_chars=3000):
    try:
        with open(path) as f:
            content = f.read(max_chars)
        sections.append(f"=== {label} ===\n{content}")
    except Exception as e:
        sections.append(f"=== {label} ===\n[could not read: {e}]")

read_file(STATE_FILE, "STATE.md (current run state)")
read_file(TASK_QUEUE, "Task Queue")
read_file(PROJECTS, "Standing Projects (excerpt)", max_chars=1500)

context = "\n\n".join(sections)

msg = (
    "PRE-REFLECT CONTEXT INJECTION\n"
    "The following system state has been auto-injected because you invoked /reflect.\n"
    "Use it to inform your reflection without needing to read these files manually.\n\n"
    + context
)

print(json.dumps({
    "continue": True,
    "suppressOutput": True,
    "systemMessage": msg
}))
