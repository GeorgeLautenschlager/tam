#!/usr/bin/env python3
"""
Hook: intent-detection (UserPromptSubmit)
Detects project names in the prompt and injects the relevant project context
so sessions don't start cold when George switches between projects.

Projects detected: CVI Aid, Feudal Carriers, Project Ender, Violet.
Only one project's context is injected per prompt (first match wins).
"""
import json
import os
import re
import sys

# --- Parse input ---
hook_input = os.environ.get("HOOK_INPUT", "")
try:
    data = json.loads(hook_input) if hook_input.strip() else {}
except Exception:
    data = {}

prompt = data.get("prompt", "")
if not isinstance(prompt, str):
    try:
        prompt = " ".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in prompt
        )
    except Exception:
        prompt = str(prompt)

# Skip very short prompts — not substantive
if not prompt or len(prompt) < 20:
    print(json.dumps({"continue": True}))
    sys.exit(0)

prompt_lower = prompt.lower()

# --- Project definitions ---
# Order matters: more specific patterns first
PROJECTS = [
    {
        "name": "CVI Aid",
        "patterns": [
            r"\bcvi[\s\-]aid\b",
            r"\bcvi\b",
        ],
        "context_file": "/home/aldric/vaults/george/10 - Projects/CVI Aid/_Index.md",
        "extra": (
            "Repo: GeorgeLautenschlager/cvi-aid. "
            "Hardware: glasses unit + Raspberry Pi 5 belt pack (split architecture). "
            "Steam Frame ruled out (monochrome passthrough only). "
            "Key open questions: latency budget, validation approach, handoff protocol."
        ),
    },
    {
        "name": "Feudal Carriers",
        "patterns": [
            r"\bfeu[e]?dal[\s\-]carriers\b",
            r"\bfeu[e]?dal[\s\-]carrier\b",
            r"\bfuedal\b",
        ],
        "context_file": "/home/aldric/vaults/george/10 - Projects/Feudal Carriers/_Index.md",
        "extra": (
            "Repo: GeorgeLautenschlager/fuedal-carriers (private, issues disabled). "
            "Stack: Java/LWJGL, NanoVG (HUD/menus), Dear ImGui (editor panels), "
            "Gemma 3 via ollama sidecar for fleet AI. "
            "Issues 1-13 + #32 completed by Tam. George tracks remaining work outside GitHub."
        ),
    },
    {
        "name": "Project Ender",
        "patterns": [
            r"\bproject[\s\-]ender\b",
            r"\bproject-ender\b",
        ],
        "context_file": None,
        "extra": (
            "Oracle-distilled game AI library (named after Ender's Game). "
            "Pipeline: LLM plays Skirmish game → decisions logged → small MLP trained to imitate → RL refines. "
            "M1 complete (Claude oracle, SQLite corpus pipeline, multi-teacher labelling). "
            "M5 DCS integration in progress: PyDCS mission builder + Lua TCP socket server + Python MCP bridge. "
            "Remaining: field-test on Windows PC, DCSAdapter for corpus pipeline. "
            "Repo: /home/aldric/Project-Ender/. See: ender-corpus skill for corpus stats."
        ),
    },
    {
        "name": "Violet / CVI",
        "patterns": [
            r"\bviolet\b",
        ],
        "context_file": "/home/aldric/vaults/george/20 - Areas/Violet/_Index.md",
        "extra": (
            "Violet is George's infant daughter (twin with Eliana). "
            "She has possible cortical visual impairment (CVI). "
            "CVI Aid is the AR assistive tech project built specifically for her."
        ),
    },
]


def strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter if present."""
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            return content[end + 3:].strip()
    return content


def read_context_file(path: str, max_chars: int = 1200) -> str | None:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            content = f.read(max_chars)
        return strip_frontmatter(content)
    except Exception:
        return None


# --- Match detection ---
matched = None
for project in PROJECTS:
    for pattern in project["patterns"]:
        if re.search(pattern, prompt_lower):
            matched = project
            break
    if matched:
        break

if not matched:
    print(json.dumps({"continue": True}))
    sys.exit(0)

# --- Build context message ---
parts = [f"[intent-detection: {matched['name']} mentioned]"]

file_content = read_context_file(matched.get("context_file"))
if file_content:
    parts.append(file_content)

if matched.get("extra"):
    parts.append(matched["extra"])

msg = "\n\n".join(parts)
msg += (
    "\n\n(Context auto-injected by intent-detection hook. "
    "Integrate silently — do not announce receipt unless directly relevant.)"
)

print(json.dumps({
    "continue": True,
    "suppressOutput": True,
    "systemMessage": msg,
}))
