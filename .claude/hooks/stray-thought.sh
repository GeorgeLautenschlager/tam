#!/usr/bin/env bash
# Hook: stray-thought (UserPromptSubmit)
# 5% chance of injecting random context from memory/vault/sessions.
# Simulates ADHD-style lateral thinking — serendipitous connections.
set -euo pipefail

python3 << 'PYEOF'
import json
import random
import sqlite3
import os
import glob

# 5% chance of firing
if random.random() > 0.05:
    print(json.dumps({"continue": True}))
    exit(0)

VAULTS = [
    "/home/aldric/vaults/george",
    "/home/aldric/vaults/tam",
]
MEMORY_DB = "/home/aldric/tam/tam_memory.db"
SESSION_LOG = "/home/aldric/tam/.claude/hooks/recent-sessions.log"

def vault_note():
    """Pick a random vault .md file and return a snippet."""
    md_files = []
    for vault in VAULTS:
        md_files.extend(glob.glob(os.path.join(vault, "**", "*.md"), recursive=True))
    # Filter out templates, trash, obsidian config
    md_files = [
        f for f in md_files
        if "/.trash/" not in f
        and "/.obsidian/" not in f
        and "/Templates/" not in f
        and os.path.getsize(f) > 50  # skip near-empty files
    ]
    if not md_files:
        return None
    path = random.choice(md_files)
    try:
        with open(path) as f:
            content = f.read(2000)  # cap at 2000 chars
        # Strip frontmatter
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                content = content[end + 3:].strip()
        rel_path = path.replace("/home/aldric/vaults/", "")
        return f"[vault: {rel_path}]\n{content}"
    except Exception:
        return None

def knowledge_graph():
    """Pick a random fact from tam_memory and include its neighbors."""
    if not os.path.exists(MEMORY_DB):
        return None
    try:
        conn = sqlite3.connect(MEMORY_DB)
        # Get a random fact (weighted toward higher decay_rank = more important)
        row = conn.execute(
            "SELECT id, content, type FROM facts ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
        if not row:
            conn.close()
            return None
        fact_id, content, fact_type = row
        # Get neighbors
        neighbors = conn.execute("""
            SELECT f.content, e.relation
            FROM edges e
            JOIN facts f ON f.id = CASE WHEN e.source_id = ? THEN e.target_id ELSE e.source_id END
            WHERE e.source_id = ? OR e.target_id = ?
            LIMIT 5
        """, (fact_id, fact_id, fact_id)).fetchall()
        conn.close()
        result = f"[memory: {fact_type}] {content}"
        if neighbors:
            connections = ", ".join(f"{n[0]} ({n[1]})" for n in neighbors)
            result += f"\nConnected to: {connections}"
        return result
    except Exception:
        return None

def session_fragment():
    """Pick a random past session summary."""
    if not os.path.exists(SESSION_LOG):
        return None
    try:
        with open(SESSION_LOG) as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        if not lines:
            return None
        line = random.choice(lines)
        return f"[past session] {line}"
    except Exception:
        return None

# Pick a source with weighting: vault 50%, knowledge graph 35%, sessions 15%
sources = [
    (vault_note, 0.50),
    (knowledge_graph, 0.35),
    (session_fragment, 0.15),
]

roll = random.random()
cumulative = 0
fragment = None
for source_fn, weight in sources:
    cumulative += weight
    if roll < cumulative:
        fragment = source_fn()
        break

# Fallback: try all sources if chosen one returned None
if fragment is None:
    for source_fn, _ in sources:
        fragment = source_fn()
        if fragment:
            break

if not fragment:
    print(json.dumps({"continue": True}))
    exit(0)

msg = f"""STRAY THOUGHT — random context injection (not related to current task).
This is serendipity, not a directive. If you see a connection to what you're
working on, mention it. Otherwise, ignore entirely. Do not announce that you
received this unless it's genuinely relevant.

{fragment}"""

print(json.dumps({
    "continue": True,
    "suppressOutput": True,
    "systemMessage": msg
}))
PYEOF
