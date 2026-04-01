#!/usr/bin/env python3
"""
tam-dayplan.py — Morning day plan generator

Scans the vault for project next actions, loose actions, and routines,
then posts a formatted day plan to Discord.

Usage:
    python3 tam-dayplan.py                # 7:30am personal day plan
    python3 tam-dayplan.py --with-work    # 8:00am combined plan (checks #work-day-priorities)
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ── Config ──────────────────────────────────────────────────────────────────

VAULT = Path(os.environ.get("VAULT_PATH", Path.home() / "vaults" / "george"))
PROJECTS_DIR = VAULT / "10 - Projects"
INBOX_DIR = VAULT / "00 - Inbox"
ROUTINES_DIR = VAULT / "20 - Areas" / "Routines"
SOMEDAY_MAYBE = VAULT / "30 - Resources" / "Someday Maybe.md"

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
DAY_PLAN_CHANNEL = os.environ.get("DISCORD_DAY_PLAN_CHANNEL", "1484955163797098517")
WORK_PRIORITIES_CHANNEL = os.environ.get("DISCORD_WORK_PRIORITIES_CHANNEL", "1484993580941705276")

ET = timezone(timedelta(hours=-4))  # Eastern Time


# ── Vault Scanning ──────────────────────────────────────────────────────────

def scan_project_next_actions() -> list[dict]:
    """Scan all project _Index.md files for next actions."""
    results = []
    if not PROJECTS_DIR.exists():
        return results

    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        index_file = project_dir / "_Index.md"
        if not index_file.exists():
            continue

        project_name = project_dir.name
        content = index_file.read_text()

        # Extract next action — format is "**Next action:** value"
        next_action = None
        for line in content.splitlines():
            stripped = line.strip()
            # Match both "**Next action:** X" and "Next action: X"
            cleaned = stripped.replace("**", "").replace("*", "")
            if cleaned.lower().startswith("next action:"):
                value = cleaned.split(":", 1)[1].strip()
                if value:
                    next_action = value
                break

        # Extract status — format is "**Current phase:** value"
        status = None
        for line in content.splitlines():
            stripped = line.strip()
            cleaned = stripped.replace("**", "").replace("*", "")
            if cleaned.lower().startswith("current phase:"):
                value = cleaned.split(":", 1)[1].strip()
                if value:
                    status = value
                break

        results.append({
            "project": project_name,
            "next_action": next_action,
            "status": status,
        })

    return results


def scan_loose_actions() -> list[str]:
    """Scan Loose Actions.md for unchecked tasks."""
    loose_file = INBOX_DIR / "Loose Actions.md"
    if not loose_file.exists():
        return []

    actions = []
    for line in loose_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("- [ ]"):
            task = line[5:].strip()
            if task:
                actions.append(task)
    return actions


def scan_routines() -> dict:
    """Scan Routines/_Index.md for today's routine items, grouped by time block."""
    index_file = ROUTINES_DIR / "_Index.md"
    if not index_file.exists():
        return {}

    content = index_file.read_text()

    # Map vault section headers to time block keys
    # Format: (block_key, start_hour, end_hour)
    BLOCK_MAP = {
        "morning": ("morning", 7, 8),
        "work": ("work", 8, 16),
        "family": ("family", 16, 19),
        "evening": ("evening", 19, 22),
    }

    result = {"morning": [], "work": [], "family": [], "evening": [], "weekly": []}
    current_block = None

    for line in content.splitlines():
        lower = line.strip().lower()

        # Detect section headers
        if lower.startswith("## weekly"):
            current_block = "weekly"
            continue
        if lower.startswith("## daily"):
            current_block = None  # sub-sections will set it
            continue
        if lower.startswith("##"):
            current_block = None
            continue

        # Detect bold sub-headers like **Morning (6–8am)**
        bold_match = re.match(r'\*\*(\w+)', lower)
        if bold_match:
            word = bold_match.group(1)
            for key in BLOCK_MAP:
                if word.startswith(key):
                    current_block = key
                    break
            continue

        stripped = line.strip()
        if stripped.startswith("- ") and current_block:
            item = stripped[2:].strip()
            # Strip checkbox prefix from weekly items
            if item.startswith("[ ] "):
                item = item[4:]
            if item:
                result[current_block].append(item)

    return result


def scan_stale_projects(projects: list[dict], days_threshold: int = 14) -> list[str]:
    """Flag projects with no next action set."""
    stale = []
    for p in projects:
        if not p["next_action"] and p["status"] and "complete" not in p["status"].lower():
            stale.append(p["project"])
    return stale


# ── Discord ─────────────────────────────────────────────────────────────────

def fetch_recent_messages(channel_id: str, limit: int = 10) -> list[dict]:
    """Fetch recent messages from a Discord channel."""
    if not DISCORD_TOKEN:
        return []
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    resp = requests.get(
        f"https://discord.com/api/v10/channels/{channel_id}/messages?limit={limit}",
        headers=headers,
    )
    if resp.status_code != 200:
        print(f"Warning: Failed to fetch messages from channel {channel_id}: {resp.status_code}", file=sys.stderr)
        return []
    return resp.json()


def get_work_priorities() -> list[str]:
    """Check #work-day-priorities for today's work items."""
    messages = fetch_recent_messages(WORK_PRIORITIES_CHANNEL, limit=20)
    today = datetime.now(ET).strftime("%Y-%m-%d")
    yesterday = (datetime.now(ET) - timedelta(days=1)).strftime("%Y-%m-%d")

    priorities = []
    for msg in messages:
        # Only include messages from the last ~18 hours (night before or morning of)
        msg_time = datetime.fromisoformat(msg["timestamp"].replace("+00:00", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - msg_time).total_seconds() / 3600
        if age_hours > 18:
            continue
        # Skip bot messages
        if msg.get("author", {}).get("bot"):
            continue
        content = msg.get("content", "").strip()
        if content:
            priorities.append(content)

    return priorities


def post_to_discord(channel_id: str, content: str) -> bool:
    """Post a message to a Discord channel. Splits if over 2000 chars."""
    if not DISCORD_TOKEN:
        print("No DISCORD_TOKEN — printing to stdout instead", file=sys.stderr)
        print(content)
        return False

    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "Content-Type": "application/json",
    }

    # Split into 2000-char chunks at newlines
    chunks = []
    while content:
        if len(content) <= 2000:
            chunks.append(content)
            break
        split_at = content.rfind("\n", 0, 2000)
        if split_at == -1:
            split_at = 2000
        chunks.append(content[:split_at])
        content = content[split_at:].lstrip("\n")

    for chunk in chunks:
        resp = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers=headers,
            json={"content": chunk},
        )
        if resp.status_code not in (200, 201):
            print(f"Discord post failed: {resp.status_code} {resp.text}", file=sys.stderr)
            return False
    return True


# ── Time Estimation ─────────────────────────────────────────────────────────

def parse_item_meta(text: str) -> tuple[str, int, int | None]:
    """Extract time estimate and optional anchor from text.

    Supports:
        'Do the thing (30m)'           → ('Do the thing', 30, None)
        'Lunch (@ 11:30) (30m)'        → ('Lunch', 30, 690)
        'Exercise (@ 09:00) (60m)'     → ('Exercise', 60, 540)

    Returns (cleaned_text, minutes, anchor_minutes_from_midnight_or_None).
    minutes=0 means "use caller default".
    """
    cleaned = text
    minutes = 0
    anchor = None

    # Extract (Xm) duration
    dur_match = re.search(r'\((\d+)m\)\s*$', cleaned)
    if dur_match:
        minutes = int(dur_match.group(1))
        cleaned = cleaned[:dur_match.start()].rstrip()
        minutes = ((minutes + 14) // 15) * 15
        minutes = max(minutes, 15)

    # Extract (@ HH:MM) anchor
    anc_match = re.search(r'\(@\s*(\d{1,2}):(\d{2})\)', cleaned)
    if anc_match:
        anchor = int(anc_match.group(1)) * 60 + int(anc_match.group(2))
        cleaned = (cleaned[:anc_match.start()] + cleaned[anc_match.end():]).strip()

    return cleaned, minutes, anchor


def _make_routine_item(text: str, block: str) -> dict:
    cleaned, est, anchor = parse_item_meta(text)
    return {
        "category": block.title(),
        "task": cleaned,
        "minutes": est or 15,
        "blocked": False,
        "anchor": anchor,
    }


def _make_task_items(
    projects: list[dict],
    loose_actions: list[str],
    work_priorities: list[str] | None = None,
) -> list[dict]:
    """Build productive task items (work, projects, loose actions)."""
    items = []

    if work_priorities:
        for wp in work_priorities:
            for sub in wp.splitlines():
                sub = sub.strip()
                if not sub:
                    continue
                cleaned, est, _ = parse_item_meta(sub)
                items.append({"category": "Work", "task": cleaned, "minutes": est or 30, "blocked": False})

    active = [p for p in projects if p["next_action"]]
    for p in active:
        blocked = p["next_action"].startswith("(blocked")
        cleaned, est, _ = parse_item_meta(p["next_action"])
        items.append({"category": p["project"], "task": cleaned, "minutes": est or 30, "blocked": blocked})

    for action in loose_actions:
        cleaned, est, _ = parse_item_meta(action)
        items.append({"category": "Loose", "task": cleaned, "minutes": est or 15, "blocked": False})

    return items


# ── Day Plan Formatting ─────────────────────────────────────────────────────

# Time blocks define the structure of the day.
# Productive tasks fill the "work" and "evening" gaps between routines.
TIME_BLOCKS = [
    # (block_key, start_hour, end_hour, label)
    ("morning", 7, 8, "Morning"),
    ("work", 8, 16, "Work Day"),
    ("family", 16, 19, "Family"),
    ("evening", 19, 22, "Evening"),
]


def format_day_plan(
    projects: list[dict],
    loose_actions: list[str],
    routines: dict,
    stale_projects: list[str],
    work_priorities: list[str] | None = None,
) -> str:
    """Format a woven day plan: routines + productive tasks in 15-min time blocks."""
    now = datetime.now(ET)
    day_name = now.strftime("%A")
    date_str = now.strftime("%B %d, %Y")

    # Build productive task pool (work > projects > loose)
    all_tasks = _make_task_items(projects, loose_actions, work_priorities)
    blocked_items = [t for t in all_tasks if t["blocked"]]
    task_pool = [t for t in all_tasks if not t["blocked"]]

    # Build weekly routine items for sidebar
    weekly_items = [r.lstrip("[ ] ") for r in routines.get("weekly", [])]

    # Walk through the day block by block, weaving routines and tasks
    lines = [f"**{day_name}, {date_str}**", ""]
    table_lines = []
    task_idx = 0  # pointer into task_pool
    total_tasks = 0

    # Max line width ~60 chars to fit Discord mobile
    MAX_TASK_LEN = 34
    CAT_WIDTH = 10

    def _fmt_row(cursor_dt, item, cat_label=None):
        """Format a single schedule row. Returns (line_str, new_cursor)."""
        start_str = cursor_dt.strftime("%H:%M")
        end_dt = cursor_dt + timedelta(minutes=item["minutes"])
        end_str = end_dt.strftime("%H:%M")
        cat = (cat_label or item.get("category", ""))[:CAT_WIDTH].ljust(CAT_WIDTH)
        task = item["task"]
        if len(task) > MAX_TASK_LEN:
            task = task[:MAX_TASK_LEN - 1] + "~"
        return f"[ ] {start_str}-{end_str} {cat} {task}", end_dt

    def _fill_tasks(cursor_dt, until_dt):
        """Fill time gap with productive tasks from the pool. Returns new cursor."""
        nonlocal task_idx, total_tasks
        while cursor_dt < until_dt and task_idx < len(task_pool):
            task = task_pool[task_idx]
            # Don't start a task if it would overflow the gap by more than 15 min
            if cursor_dt + timedelta(minutes=task["minutes"]) > until_dt + timedelta(minutes=15):
                break
            row, cursor_dt = _fmt_row(cursor_dt, task)
            table_lines.append(row)
            task_idx += 1
            total_tasks += 1
        return cursor_dt

    for block_key, start_h, end_h, label in TIME_BLOCKS:
        block_start = now.replace(hour=start_h, minute=0, second=0, microsecond=0)
        block_end = now.replace(hour=end_h, minute=0, second=0, microsecond=0)
        cursor = block_start

        # Section divider
        table_lines.append(f"--- {label} ({start_h:02d}:00-{end_h:02d}:00) ---")

        # Get routine items for this block, split into anchored and unanchored
        block_routines = [_make_routine_item(r, block_key) for r in routines.get(block_key, [])]
        anchored = sorted(
            [r for r in block_routines if r["anchor"] is not None],
            key=lambda r: r["anchor"],
        )
        unanchored = [r for r in block_routines if r["anchor"] is None]

        # Schedule unanchored routines first (they go at block start)
        for routine in unanchored:
            if cursor >= block_end:
                break
            row, cursor = _fmt_row(cursor, routine, "Routine")
            table_lines.append(row)
            total_tasks += 1

        # Now interleave: fill with tasks until next anchor, place anchor, repeat
        can_fill = block_key in ("work", "evening")

        for anchored_item in anchored:
            anchor_dt = now.replace(
                hour=anchored_item["anchor"] // 60,
                minute=anchored_item["anchor"] % 60,
                second=0, microsecond=0,
            )
            # Fill gap before this anchor with productive tasks
            if can_fill and cursor < anchor_dt:
                cursor = _fill_tasks(cursor, anchor_dt)
            # Advance cursor to anchor time if we're behind
            if cursor < anchor_dt:
                cursor = anchor_dt
            # Place the anchored routine
            row, cursor = _fmt_row(cursor, anchored_item, "Routine")
            table_lines.append(row)
            total_tasks += 1

        # Fill remaining block time with productive tasks
        if can_fill:
            cursor = _fill_tasks(cursor, block_end)

        table_lines.append("")

    # Assemble output — split into per-section code blocks so the
    # 2000-char Discord splitter can break between sections cleanly.
    section_buf = []
    for tl in table_lines:
        if tl.startswith("--- ") and section_buf:
            # Flush previous section as its own code block
            lines.append("```")
            lines.extend(section_buf)
            lines.append("```")
            section_buf = [tl]
        else:
            section_buf.append(tl)
    if section_buf:
        lines.append("```")
        lines.extend(section_buf)
        lines.append("```")

    lines.append(f"*{total_tasks} items scheduled*")

    # Any tasks that didn't fit
    overflow = task_pool[task_idx:]
    if overflow:
        lines.append("")
        lines.append(f"**Overflow** ({len(overflow)} tasks didn't fit):")
        for t in overflow:
            lines.append(f"- {t['category']}: {t['task']}")

    if weekly_items:
        lines.append("")
        lines.append("**Weekly recurring:**")
        for item in weekly_items:
            lines.append(f"- {item}")

    if blocked_items:
        lines.append("")
        lines.append("**Blocked:**")
        for i in blocked_items:
            lines.append(f"- {i['category']}: {i['task']}")

    if stale_projects:
        lines.append("")
        lines.append("**Needs attention** (no next action):")
        for name in stale_projects:
            lines.append(f"- {name}")

    return "\n".join(lines)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tam day plan generator")
    parser.add_argument("--with-work", action="store_true",
                        help="Include work priorities from Discord channel")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan to stdout without posting to Discord")
    args = parser.parse_args()

    # Load .env if DISCORD_TOKEN isn't set
    global DISCORD_TOKEN
    if not DISCORD_TOKEN:
        env_file = Path.home() / "tam" / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("DISCORD_TOKEN="):
                    DISCORD_TOKEN = line.split("=", 1)[1].strip().strip("'\"")

    # Scan vault
    projects = scan_project_next_actions()
    loose_actions = scan_loose_actions()
    routines = scan_routines()
    stale = scan_stale_projects(projects)

    # Work priorities (8am run only)
    work_priorities = None
    if args.with_work:
        work_priorities = get_work_priorities()

    # Format
    plan = format_day_plan(projects, loose_actions, routines, stale, work_priorities)

    if args.dry_run:
        print(plan)
    else:
        success = post_to_discord(DAY_PLAN_CHANNEL, plan)
        if success:
            label = "combined" if args.with_work else "personal"
            print(f"Day plan ({label}) posted to #day-plan")
        else:
            print("Failed to post day plan", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
