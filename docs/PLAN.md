# tam-memory MCP — Implementation Plan

## Goal
Build an MCP server that gives Tam (across Claude Code and Discord sessions) unified access to a persistent memory system combining keyword search, vector search, and a knowledge graph. Add a session transcript ingestion pipeline that extracts structured knowledge from past conversations.

## Architecture

```
┌──────────────────────────────────────────────┐
│              tam-memory MCP                  │
│         (Python, stdio transport)            │
├──────────────────────────────────────────────┤
│  Tools:                                      │
│   • recall(query, top_k, scope)              │
│   • remember(content, type, entities, rels)  │
│   • graph_traverse(entity, depth, rel_type)  │
│   • ingest_session(session_id)               │
│   • list_entities(type_filter)               │
│                                              │
│  Resources:                                  │
│   • recent_context — latest facts & activity │
├──────────┬───────────┬───────────────────────┤
│ FTS5/BM25│ sqlite_vec│ networkx (SQLite-     │
│ (keyword)│ (vector)  │ backed graph)         │
└──────────┴───────────┴───────────────────────┘
         All in one SQLite DB: tam_memory.db
```

## Components

### 1. Database: `tam_memory.db`

Single SQLite database with three layers:

**Facts table** — extracted knowledge units:
```sql
CREATE TABLE facts (
    id INTEGER PRIMARY KEY,
    content TEXT,           -- the fact/decision/concept
    type TEXT,              -- entity | decision | concept | preference | observation
    source TEXT,            -- session_id, "manual", "discord"
    source_timestamp TEXT,  -- when the source event occurred
    created_at TEXT,
    decay_rank REAL DEFAULT 1.0  -- down-ranked over time, never deleted
);
```

**FTS index** on facts:
```sql
CREATE VIRTUAL TABLE facts_fts USING fts5(content, type, content=facts, content_rowid=id);
```

**Vector embeddings** on facts:
```sql
CREATE VIRTUAL TABLE facts_vec USING vec0(embedding float[768]);
```

**Graph edges** — relationships between facts:
```sql
CREATE TABLE edges (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES facts(id),
    target_id INTEGER REFERENCES facts(id),
    relation TEXT,          -- evokes, informs, contradicts, depends_on, supersedes, etc.
    weight REAL DEFAULT 1.0,
    created_at TEXT
);
CREATE INDEX idx_edges_source ON edges(source_id);
CREATE INDEX idx_edges_target ON edges(target_id);
CREATE INDEX idx_edges_relation ON edges(relation);
```

### 2. MCP Server: `tam-memory-mcp.py`

Python script using the `mcp` Python SDK (FastMCP). Stdio transport.

**Tools exposed:**

| Tool | Purpose |
|------|---------|
| `recall(query, top_k=5, scope=null)` | Hybrid search: BM25 + vector + graph boost. Returns ranked facts with related entities. |
| `remember(content, type, entities=[], relations=[])` | Store a new fact, embed it, optionally link to existing entities. |
| `graph_traverse(entity, depth=2, rel_type=null)` | Walk the graph from an entity. Returns connected facts and relationship paths. |
| `ingest_session(session_id)` | Process a session JSONL → extract facts and relationships via Haiku. |
| `list_entities(type_filter=null)` | List known entities in the graph, optionally filtered by type. |

**Resources exposed:**

| Resource | Purpose |
|----------|---------|
| `recent_context` | Read-only resource returning recent facts, decisions, and activity from the last 48h. Referenced in BOOT.md so cron-Tam can optionally load it. Interactive sessions can pull it at startup for continuity. |

**Recall algorithm** (reusing tam-search.py pattern):
1. BM25 search via FTS5 → ranked list
2. Vector similarity via sqlite_vec → ranked list
3. RRF merge (k=60) → combined ranking
4. Graph boost: for top results, check if query entities connect to result entities in graph → boost score
5. Return top_k with snippets and related entities

### 3. Session Ingest Pipeline: `tam-ingest.py`

Standalone script (also callable via MCP tool). Processes both Claude Code session JSONL files and Discord logs.

**Sources:**
- Claude Code sessions: `/home/aldric/.claude/projects/-home-aldric-tam/*.jsonl` (152 files, 16MB)
- Discord logs: `/home/aldric/tam/logs/discord.log` (142KB)

**Pipeline:**
1. Parse JSONL (or Discord log format) → extract user messages and assistant text responses (skip tool calls, thinking blocks)
2. Chunk into conversation segments (~10 turns each)
3. Send each chunk to Haiku with extraction prompt:
   - Extract: entities (people, projects, systems, files), decisions, concepts, preferences
   - Extract: relationships between entities (with relation type and direction)
   - Output: structured JSON
4. Deduplicate against existing facts (embedding similarity > 0.9 = likely duplicate)
5. Store facts, create embeddings, insert graph edges
6. Mark session as ingested in a `sessions_ingested` table

**Extraction prompt** (Haiku):
```
Given this conversation segment between a user (George) and an AI assistant (Tam),
extract structured knowledge:

1. Facts: decisions made, preferences stated, concepts discussed, observations noted
2. Entities: people, projects, systems, tools, files mentioned
3. Relationships: how entities and concepts connect to each other

Output JSON:
{
  "facts": [{"content": "...", "type": "decision|concept|preference|observation", "entities": ["..."]}],
  "relationships": [{"source": "...", "target": "...", "relation": "evokes|informs|depends_on|..."}]
}
```

**Cost estimate:** 152 Claude Code sessions + Discord logs × ~3 chunks avg × ~2K tokens/chunk ≈ $0.25 total via Haiku

### 4. Registration

Add to Claude Code config (`~/.claude/settings.json` or project `.mcp.json`):
```json
{
  "mcpServers": {
    "tam-memory": {
      "command": "/home/aldric/tam/.venv/bin/python",
      "args": ["/home/aldric/tam/tam-memory-mcp.py"]
    }
  }
}
```

## Implementation Steps

### Phase 1: Database + Core MCP (day 1)
1. Install `mcp` Python SDK into tam venv (`pip install mcp`)
2. Create `tam_memory.db` with schema (facts, facts_fts, facts_vec, edges, sessions_ingested)
3. Build `tam-memory-mcp.py` with `recall` and `remember` tools
4. Implement hybrid search (port RRF logic from tam-search.py)
5. Register MCP with Claude Code
6. Smoke test: manually `remember` a few facts, `recall` them

### Phase 2: Knowledge Graph (day 1-2)
7. Add `graph_traverse` and `list_entities` tools
8. Load edges into networkx at startup, rebuild on writes
9. Implement graph boost in recall algorithm
10. Test associative traversal: "what connects Project Ender to Tam's memory system?"

### Phase 3: Session Ingestion (day 2)
11. Build `tam-ingest.py` extraction pipeline
12. Write Haiku extraction prompt, test on 2-3 sessions manually
13. Add deduplication logic (embedding similarity check)
14. Wire up `ingest_session` MCP tool
15. Batch-ingest all 152 historical Claude Code sessions + Discord logs

### Phase 4: Integration + Measurement (day 3)
16. Add ingestion to cron (process new Claude Code sessions + Discord logs nightly)
17. Import existing memory files (feedback_*.md, project_*.md, etc.) as seed facts
18. Define measurement baseline:
    - Cold start test: can Tam answer "what did we work on yesterday?" without file reads?
    - Redundant correction count (manual tracking)
    - Context re-discovery tool calls at session start
19. Add `recent_context` resource reference to BOOT.md
20. Document in CLAUDE.md how/when to use recall

## Dependencies
- `mcp` (Python MCP SDK — FastMCP)
- `sqlite_vec` (already installed)
- `networkx` (new — `pip install networkx`)
- `ollama` for embeddings (already running nomic-embed-text)
- Anthropic API for Haiku extraction (already have key via claude CLI)

## Files Created
- `/home/aldric/tam/tam-memory-mcp.py` — MCP server
- `/home/aldric/tam/tam-ingest.py` — session extraction pipeline
- `/home/aldric/tam/tam_memory.db` — unified memory database

## Decisions Made
- **Discord ingestion:** Yes — ingest Discord logs alongside Claude Code sessions
- **Context access:** MCP exposes a `recent_context` resource (pull, not push). BOOT.md references it so cron-Tam can optionally load it. Interactive sessions pull at startup for continuity.
- **TTL policy:** No expiry. Down-rank old observations over time but keep everything indefinitely. Storage is cheap.
