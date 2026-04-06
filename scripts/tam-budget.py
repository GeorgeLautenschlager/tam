#!/usr/bin/env python3
"""tam-budget.py — Budget checker for Tam's cron and Discord systems.

Parses USAGE.log, aggregates daily/weekly spend, and returns a go/no-go
verdict as JSON. Called by tam.sh before each run and by tam-discord.py
before each Claude invocation.

Exit codes:
  0 — allowed (budget OK)
  1 — blocked (over threshold)
  2 — error (config missing, parse failure, etc.)

Usage:
  python3 tam-budget.py --source cron      # check cron budget
  python3 tam-budget.py --source discord    # check discord budget
  python3 tam-budget.py --summary           # print daily/weekly totals
"""

import argparse
import json
import re
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

TAM_HOME = Path(__file__).parent.parent  # scripts/ -> tam/
USAGE_LOG = TAM_HOME / "data" / "USAGE.log"
BUDGET_CONFIG = TAM_HOME / "data" / "budget.json"  # Already correct now

# Matches both formats:
#   2026-03-17T15:12:14.068357 | discord | cost=$0.038 | ...
#   2026-03-17_1700 | cron | cost=0.118 | ...
#   2026-03-18_1630 | cron | rate_limited | ...
LINE_RE = re.compile(
    r'^(?P<ts>\S+)\s*\|\s*(?P<source>\w+)\s*\|'
    r'(?:\s*cost=\$?(?P<cost>[\d.]+))?'
)

# Timestamp formats
TS_ISO = re.compile(r'^\d{4}-\d{2}-\d{2}T')
TS_CRON = re.compile(r'^\d{4}-\d{2}-\d{2}_\d{4}$')


def parse_timestamp(ts: str) -> datetime | None:
    """Parse either ISO or cron timestamp format."""
    try:
        if TS_ISO.match(ts):
            # Truncate microseconds if present, parse ISO
            return datetime.fromisoformat(ts)
        elif TS_CRON.match(ts):
            return datetime.strptime(ts, "%Y-%m-%d_%H%M")
    except ValueError:
        pass
    return None


def parse_usage_log() -> list[dict]:
    """Parse USAGE.log into structured entries."""
    if not USAGE_LOG.exists():
        return []

    entries = []
    for line in USAGE_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        m = LINE_RE.match(line)
        if not m:
            continue

        ts = parse_timestamp(m.group("ts"))
        if ts is None:
            continue

        cost_str = m.group("cost")
        cost = float(cost_str) if cost_str else 0.0

        entries.append({
            "timestamp": ts,
            "source": m.group("source"),
            "cost": cost,
        })

    return entries


def get_daily_total(entries: list[dict], day: date) -> float:
    """Sum costs for a given date."""
    return sum(e["cost"] for e in entries if e["timestamp"].date() == day)


def get_weekly_total(entries: list[dict], today: date) -> float:
    """Sum costs from Monday of the current week through today."""
    monday = today - timedelta(days=today.weekday())
    return sum(
        e["cost"] for e in entries
        if monday <= e["timestamp"].date() <= today
    )


def load_config() -> dict:
    """Load budget.json config."""
    if not BUDGET_CONFIG.exists():
        raise FileNotFoundError(f"Budget config not found: {BUDGET_CONFIG}")
    return json.loads(BUDGET_CONFIG.read_text())


def check_budget(source: str) -> dict:
    """Check budget and return verdict dict."""
    config = load_config()

    # Kill switch: "enabled": false in budget.json bypasses all checks
    if not config.get("enabled", True):
        return {
            "allowed": True,
            "reason": "budget_disabled",
            "source": source,
            "daily_used": 0,
            "daily_limit": config.get("daily_limit_usd", 0),
            "daily_pct": 0,
            "weekly_used": 0,
            "weekly_limit": config.get("weekly_limit_usd", 0),
            "weekly_pct": 0,
        }

    entries = parse_usage_log()
    today = date.today()

    daily_limit = config["daily_limit_usd"]
    weekly_limit = config["weekly_limit_usd"]
    threshold_pct = config["threshold_pct"]
    discord_reserve_pct = config.get("discord_reserve_pct", 20)

    daily_used = get_daily_total(entries, today)
    weekly_used = get_weekly_total(entries, today)

    daily_pct = (daily_used / daily_limit * 100) if daily_limit > 0 else 0
    weekly_pct = (weekly_used / weekly_limit * 100) if weekly_limit > 0 else 0

    # Cron checks against threshold, with discord reserve carved out
    # Discord checks against a higher threshold (95%) of the full limit
    if source == "cron":
        reserve_factor = (100 - discord_reserve_pct) / 100
        effective_daily = daily_limit * reserve_factor
        effective_weekly = weekly_limit * reserve_factor
        effective_threshold = threshold_pct
    else:  # discord
        effective_daily = daily_limit
        effective_weekly = weekly_limit
        effective_threshold = 95  # Discord gets more headroom

    daily_effective_pct = (daily_used / effective_daily * 100) if effective_daily > 0 else 0
    weekly_effective_pct = (weekly_used / effective_weekly * 100) if effective_weekly > 0 else 0

    if daily_effective_pct >= effective_threshold:
        allowed = False
        reason = "daily_threshold"
    elif weekly_effective_pct >= effective_threshold:
        allowed = False
        reason = "weekly_threshold"
    else:
        allowed = True
        reason = "ok"

    return {
        "allowed": allowed,
        "daily_used": round(daily_used, 4),
        "daily_limit": daily_limit,
        "daily_pct": round(daily_pct, 1),
        "weekly_used": round(weekly_used, 4),
        "weekly_limit": weekly_limit,
        "weekly_pct": round(weekly_pct, 1),
        "reason": reason,
        "source": source,
    }


def print_summary():
    """Print a human-readable budget summary."""
    config = load_config()
    entries = parse_usage_log()
    today = date.today()

    daily_used = get_daily_total(entries, today)
    weekly_used = get_weekly_total(entries, today)

    # Per-source breakdown for today
    daily_cron = sum(e["cost"] for e in entries if e["timestamp"].date() == today and e["source"] == "cron")
    daily_discord = sum(e["cost"] for e in entries if e["timestamp"].date() == today and e["source"] == "discord")

    print(f"Budget Summary — {today}")
    print(f"  Daily:  ${daily_used:.4f} / ${config['daily_limit_usd']:.2f} ({daily_used/config['daily_limit_usd']*100:.1f}%)")
    print(f"    Cron:    ${daily_cron:.4f}")
    print(f"    Discord: ${daily_discord:.4f}")
    print(f"  Weekly: ${weekly_used:.4f} / ${config['weekly_limit_usd']:.2f} ({weekly_used/config['weekly_limit_usd']*100:.1f}%)")


def main():
    parser = argparse.ArgumentParser(description="Tam budget checker")
    parser.add_argument("--source", choices=["cron", "discord"], help="Check budget for source")
    parser.add_argument("--summary", action="store_true", help="Print human-readable summary")
    args = parser.parse_args()

    if args.summary:
        try:
            print_summary()
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(2)
        return

    if not args.source:
        parser.error("--source is required (unless using --summary)")

    try:
        result = check_budget(args.source)
        print(json.dumps(result))
        sys.exit(0 if result["allowed"] else 1)
    except Exception as e:
        error = {"allowed": False, "reason": "error", "error": str(e)}
        print(json.dumps(error))
        sys.exit(2)


if __name__ == "__main__":
    main()
