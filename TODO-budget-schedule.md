# Usage-Aware Self-Scheduling — COMPLETE

All items implemented 2026-03-25. See plan: `~/.claude/plans/sleepy-inventing-firefly.md`

## What was built
- `budget.json` — $5/day, $25/week, 80% threshold, 20% discord reserve
- `tam-budget.py` — USAGE.log parser, go/no-go JSON output, `--summary` flag
- `schedule.json` — Tam-managed schedule (60min default, quiet hours 22-07)
- `tam.sh` — pause check, schedule gate, budget gate, context injection, post-run fallback, `--force` flag
- `BOOT.md` — section 2b (budget tiers + schedule control), autonomous activity budget gate
- `tam-discord.py` — fail-open budget check, `/budget` slash command
- `crontab.txt` — `*/15 * * * *` heartbeat replaces static hourly+overnight

## To activate
George needs to install the new crontab: `crontab ~/tam/crontab.txt`
Discord bot needs restart to pick up new slash command: `systemctl --user restart tam-discord`
