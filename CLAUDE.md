# CLAUDE.md — Project conventions for Tam

## Memory MCP (`tam-memory`)

Tam has a persistent knowledge graph (2500+ facts, 4000+ edges) built from all past Claude Code sessions and Discord logs. Access it via the `tam-memory` MCP tools.

### When to use `recall`
- **Session startup** — if you need context about prior conversations, decisions, or preferences that aren't in STATE.md or vault files
- **Answering "did we...?" / "what was...?"** — when George asks about something from a past session
- **Before re-explaining** — check if a topic was already covered; avoid redundant context discovery
- **Cross-referencing** — when a current task connects to prior work in non-obvious ways

### When to use `remember`
- After substantive decisions that aren't captured elsewhere (vault, git, STATE.md)
- When George shares new preferences, constraints, or context worth preserving
- Don't duplicate what's already in vault Learned.md or STATE.md — those are authoritative

### When to use `graph_traverse`
- Exploring connections: "what's related to Project Ender?" or "what decisions involved the Discord bot?"
- Understanding context webs around a topic before making recommendations

### When NOT to use memory tools
- For information that's in the current file tree — just read the files
- For git history — use `git log` / `git blame`
- For vault content — use `tam-search.py` (BM25 + vector search over vault)
- Don't use `recall` as a replacement for reading STATE.md or BOOT.md — those are always loaded

### Ingestion
- Nightly at 3:03am: `scripts/tam-ingest-cron.sh` processes new Claude Code sessions + Discord logs
- Manual: `python3 scripts/tam-ingest.py` (new sessions), `--discord`, `--seed-memory`
- Each session is chunked and extracted via Haiku, deduplicated by embedding similarity

## File conventions
- `SOUL.md` — system prompt (identity, personality, values)
- `BOOT.md` — wake-up sequence (cron and interactive)
- `STATE.md` — hot state, overwritten each run
- `PLAN.md` — current implementation plan (if any)
- Vault (`~/vaults/tam/`) — authoritative long-term memory, task queue, projects
