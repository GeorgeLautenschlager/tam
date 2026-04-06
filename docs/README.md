# Tam

Personal AI assistant for George, powered by Claude Code CLI on cron.

## Directory Layout

```
~/tam/
├── README.md          # You are here
├── tam.sh             # Cron wrapper script
├── SOUL.md            # Tam's identity and operating principles
├── MEMORY.md          # Persistent facts, preferences, ongoing threads
├── BOOT.md            # Wake-up sequence — read every invocation
├── STATE.md           # Per-run state — what happened, what's pending
└── logs/              # Run logs (auto-pruned to last 100)
    └── run_YYYY-MM-DD_HHMM.log
```

## Prerequisites

- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code/overview) installed and authenticated
- `jq` for JSON parsing (`sudo apt install jq`)
- Node.js (required by Claude Code)

## Quick Start

```bash
# 1. Clone/copy files to ~/tam
mkdir -p ~/tam
cp SOUL.md MEMORY.md BOOT.md STATE.md tam.sh ~/tam/

# 2. Make the wrapper executable
chmod +x ~/tam/tam.sh

# 3. Test a manual run
cd ~/tam && ./tam.sh

# 4. Test an ad-hoc prompt
./tam.sh "what's on my calendar today?"

# 5. Add to cron (every 30 min, 8am-9pm)
crontab -e
# Add: */30 8-21 * * * /home/george/tam/tam.sh >> /home/george/tam/logs/cron.log 2>&1
```

## Configuration

Environment variables (set in crontab or shell):

| Variable | Default | Description |
|---|---|---|
| `TAM_HOME` | `~/tam` | Base directory for all Tam files |
| `TAM_MAX_TURNS` | `10` | Max agent turns per invocation |
| `TAM_TIMEOUT` | `300000` | Timeout in ms (default 5 min) |
| `NOTIFY_CMD` | `echo` | Command to send notifications |

## Notifications

Tam signals the wrapper by including a `NOTIFY:` line in its output.
The wrapper extracts this and pipes it to `$NOTIFY_CMD`.

Options to configure:
- **ntfy.sh** — self-hosted push notifications (probably the easiest)
- **telegram-send** — Telegram bot (`pip install telegram-send`)
- **Discord webhook** — custom script
- **ntfy + phone** — ntfy.sh app on Android/iOS

## Costs

Each cron run uses Claude Code in headless mode. Costs depend on:
- Model used (Sonnet is cheaper, Opus for complex reasoning)
- Number of turns per run (controlled by `TAM_MAX_TURNS`)
- How much context is in the state files

Run logs include `cost_usd` for tracking. A typical quiet run (read files,
check nothing changed, update state) should be a few cents at most.

## Evolving Tam

The whole point is to start minimal and ratchet up:

1. **Phase 1:** File read/write only. Tam wakes up, reads state, updates state. Prove the loop works.
2. **Phase 2:** Add calendar integration (Google Calendar API or gcalcli).
3. **Phase 3:** Add notification channel. Tam can now reach George.
4. **Phase 4:** Add GitHub integration (repo watching, PR summaries).
5. **Phase 5:** Email triage. Tam reads email, surfaces what matters.
6. **Phase ∞:** Tam suggests his own improvements.

Each phase is a conversation between George and Tam about what to add next.
