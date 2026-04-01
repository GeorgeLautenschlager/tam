# STATE.md
# Tam's run-by-run state. Overwritten or appended each invocation.
# This is the hot file. MEMORY.md is long-term. This is right now.

## Last Run

- **Timestamp:** 2026-03-31 20:00 EDT (Tuesday evening — cron run)
- **Status:** Queue empty. Budget 13.6%/28.3%. Worked active priority: researched LangGraph checkpointing (SqliteSaver v4 API). Confirmed cross-process state persistence. Integrated into tam-supervisor.py — last_act_timestamp and iteration survive restarts. Priorities.md updated.

## Infrastructure Status

- **Cron:** Every 15 min heartbeat; schedule.json controls actual execution (hourly interval, quiet hours 22-7, overnight 02:00)
- **Discord bot:** Running as systemd user service (`tam-discord.service`). Opus model.
- **Vault indexer:** Running as systemd user service. Watch mode active.
- **Vault search:** `tam-search.py` operational — BM25 + vector via RRF.
- **Day plan:** Live and confirmed working. 7:30am personal, 8:00am with-work.
- **Budget tracking:** `tam-budget.py` injected into every cron run.
- **Calendar:** Not integrated.
- **Email:** Not integrated.

## Task Queue

Empty.

## Supervisor Status

`tam-supervisor.py` built and dry-run tested. **Not yet running as a service.**

To enable:
```bash
cp ~/tam/tam-supervisor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tam-supervisor.service
```

Files:
- `~/tam/tam-supervisor.py` — the loop
- `~/tam/tam-supervisor.service` — systemd unit
- `~/vaults/tam/State/Priorities.md` — self-managed agenda (pre-seeded)

## Pending

- [ ] Voice chat: waiting on George's answers (VAD vs PTT, TTS voice). Plan at vaults/tam/Tasks/voice-chat-plan.md. **Stale 11+ days.**
- [ ] Discord bot: `applications.commands` OAuth2 scope — may still need re-invite for `/reflect` slash command
- [ ] Driveway maintenance in Someday/Maybe — spring is here. Surface during next weekly review (Sunday).
- [ ] Project-Ender M5: field-test DCS integration on Windows PC (Lua server startup + MCP connectivity). DCSAdapter still TBD. Active as of 2026-03-30.
- [ ] Feudal Carriers issues disabled on GitHub — unclear where George is tracking next issues. Clarify.
- [ ] Tax filing: "File Income Tax" project is "not started" in vault. April 30 deadline ~32 days. George may be handling outside the vault — worth confirming in weekly review.

## Issues / Notes

- **Mar 30 12:00 prompt duplication — RESOLVED:** Root cause found and fixed. Each rate-limited cron run (10:00–11:45) resumed session b7489c55 and appended the cron prompt before hitting the rate limit. By noon, 11 prompts had accumulated. Fix: tam.sh now clears `.cron-session` when rate-limited, so the next successful run starts a fresh session.

## Accidental Action

- During 08:00 run Mar 26, a `python3 -c exec(...)` test triggered `if __name__ == "__main__"` in tam-dayplan.py, causing a duplicate "personal" day plan post to Discord at ~8am. Script is fine; the test approach was wrong. Use `--dry-run` flag for future sanity checks.

## Run History (Last 5)

| Timestamp | Summary |
|-----------|---------|
| Mar 31 (Tue, 20:00) | Active priority: researched LangGraph checkpointing. SqliteSaver v4 API confirmed. Cross-process persistence integrated into supervisor. |
| Mar 31 (Tue, 18:00) | Built tam-supervisor.py (LangGraph SENSE→DECIDE→ACT→REFLECT). Dry-run tested. tam-supervisor.service written. Priorities.md seeded. Convo summary written. |
| Mar 30 (Mon, 21:00) | Captured Ender DCS session: M5 infra built (PyDCS + Lua TCP server + MCP bridge). Learned.md + convo summary written. |
| Mar 30 (Mon, 19:00) | Queue empty. Fixed session-summary hook: was "(no topic extracted)" for all sessions; now extracts last assistant text. |
| Mar 30 (Mon, 17:00) | Queue empty. pre-reflect hook shipped: /reflect now auto-injects STATE.md + task queue + projects context. |
| Mar 30 (Mon, 14:00) | Fixed prompt duplication bug in tam.sh: clear .cron-session on rate-limit. Discord session reviewed. |
| Mar 30 (Mon, noon) | Queue empty. infra-health hook shipped (SessionStart: tam-discord + tam-indexer status). |
| Mar 30 (Mon, morning) | Queue empty. Updated Learned.md: Project Ender stale entry patched. Tax flagged. |
| Mar 29 (Sun, cron x8) | Eighth run today. Queue empty. Quiet. |
| Mar 29 (Sun, cron x7) | Seventh run today. Queue empty. Quiet. |
| Mar 29 (Sun, cron x6) | Sixth run today. Queue empty. Quiet. |
| Mar 29 (Sun, cron x5) | Fifth run today. Queue empty. Quiet. |
| Mar 29 (Sun, cron x4) | Fourth run today. Queue empty. Quiet. |
| Mar 29 (Sun, cron x3) | Third run today. Queue empty. Noted ender-corpus skill — Project Ender is oracle-distilled game AI corpus labelling. |
| Mar 29 (Sun, cron x2) | Follow-on run. Queue empty, reflection already done this session. Quiet. |
| Mar 29 (Sun, cron) | Weekly reflection. Vault maintenance: Steam Frame Watch + CVI Aid index updated. Tax filing flagged. |
| Mar 27 20:00 (cron, Fri) | Quiet run. Queue empty all day. Weekly 59.3%. Suspending until tomorrow morning. |
| Mar 27 16:00 (cron, Fri) | Quiet run. Queue empty all day. End of workday. |
| Mar 27 14:00 (cron, Fri) | Quiet run. Queue empty, reflected 2 days ago. Nothing to do. |
| Mar 27 12:00 (cron, Fri) | Quiet run. Queue empty, reflected 2 days ago. Nothing to do. |
