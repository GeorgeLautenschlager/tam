#!/usr/bin/env python3
"""
tam-ingest.py — Session transcript ingestion pipeline for tam-memory

Processes Claude Code session JSONL files and Discord logs, extracts structured
knowledge (facts, entities, relationships) via Haiku, and stores them in
tam_memory.db.

Usage:
    python3 tam-ingest.py                          # ingest all new sessions
    python3 tam-ingest.py --session UUID            # ingest one session
    python3 tam-ingest.py --dry-run                 # preview without storing
    python3 tam-ingest.py --discord                 # ingest Discord logs
    python3 tam-ingest.py --seed-memory             # import existing memory .md files
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import sqlite_vec

# ── Config ───────────────────────────────────────────────────────────────────

TAM_HOME = Path(os.environ.get("TAM_HOME", Path.home() / "tam"))
DB_PATH = TAM_HOME / "tam_memory.db"
SESSION_DIR = Path.home() / ".claude" / "projects" / "-home-aldric-tam"
DISCORD_LOG = TAM_HOME / "logs" / "discord.log"
MEMORY_DIR = Path.home() / ".claude" / "projects" / "-home-aldric-tam" / "memory"
EMBED_MODEL = "nomic-embed-text"
EMBED_DIM = 768
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
HAIKU_MODEL = "claude-haiku-4-5-20251001"
CHUNK_SIZE = 10  # turns per chunk

# ── Logging ──────────────────────────────────────────────────────────────────

(TAM_HOME / "logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(TAM_HOME / "logs" / "ingest.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("tam-ingest")

# ── Database ─────────────────────────────────────────────────────────────────


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def embed(text: str) -> list[float]:
    payload = json.dumps({"model": EMBED_MODEL, "input": text}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    return result["embeddings"][0]


def store_fact(conn: sqlite3.Connection, content: str, fact_type: str,
               source: str, source_timestamp: str | None = None) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO facts(content, type, source, source_timestamp, created_at, decay_rank)
           VALUES (?, ?, ?, ?, ?, 1.0)""",
        (content, fact_type, source, source_timestamp or now, now),
    )
    fact_id = cur.lastrowid
    try:
        vector = embed(content)
        conn.execute(
            "INSERT INTO facts_vec(rowid, embedding) VALUES (?, ?)",
            (fact_id, sqlite_vec.serialize_float32(vector)),
        )
    except Exception as e:
        log.warning(f"Embedding failed for fact {fact_id}: {e}")
    conn.commit()
    return fact_id


def store_edge(conn: sqlite3.Connection, source_id: int, target_id: int,
               relation: str, weight: float = 1.0) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO edges(source_id, target_id, relation, weight, created_at) VALUES (?, ?, ?, ?, ?)",
        (source_id, target_id, relation, weight, now),
    )
    conn.commit()


def find_similar_fact(conn: sqlite3.Connection, content: str,
                      threshold: float = 0.15) -> int | None:
    """Find existing fact by embedding distance. Returns fact ID if similar enough."""
    try:
        vec = embed(content)
    except Exception:
        return None
    row = conn.execute(
        """SELECT v.rowid, v.distance FROM facts_vec v
           WHERE v.embedding MATCH ? AND k = 1""",
        (sqlite_vec.serialize_float32(vec),),
    ).fetchone()
    if row and row["distance"] < threshold:
        return row["rowid"]
    return None


def find_or_create_entity(conn: sqlite3.Connection, name: str,
                          source: str) -> int:
    """Find an existing entity by name, or create one."""
    row = conn.execute(
        "SELECT id FROM facts WHERE type = 'entity' AND content = ? LIMIT 1",
        (name,),
    ).fetchone()
    if row:
        return row["id"]
    # Try fuzzy match
    row = conn.execute(
        "SELECT id FROM facts WHERE type = 'entity' AND content LIKE ? LIMIT 1",
        (f"%{name}%",),
    ).fetchone()
    if row:
        return row["id"]
    return store_fact(conn, name, "entity", source)


# ── JSONL Parsing ────────────────────────────────────────────────────────────


def parse_session_jsonl(path: Path) -> list[dict]:
    """Parse a session JSONL into a flat list of conversation turns."""
    turns = []
    with open(path) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("type") == "user":
                msg = entry.get("message", {})
                content = msg.get("content", "")
                # Handle tool_result content (list format)
                if isinstance(content, list):
                    continue  # skip tool results
                if isinstance(content, str) and content.strip():
                    turns.append({
                        "role": "user",
                        "content": content.strip(),
                        "timestamp": entry.get("timestamp", ""),
                    })

            elif entry.get("type") == "assistant":
                msg = entry.get("message", {})
                content_blocks = msg.get("content", [])
                if isinstance(content_blocks, list):
                    text_parts = []
                    for block in content_blocks:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                    if text_parts:
                        turns.append({
                            "role": "assistant",
                            "content": "\n".join(text_parts).strip(),
                            "timestamp": entry.get("timestamp", ""),
                        })

    return turns


def chunk_turns(turns: list[dict], chunk_size: int = CHUNK_SIZE) -> list[list[dict]]:
    """Split turns into chunks for extraction."""
    chunks = []
    for i in range(0, len(turns), chunk_size):
        chunk = turns[i:i + chunk_size]
        if chunk:
            chunks.append(chunk)
    return chunks


def format_chunk_for_extraction(chunk: list[dict]) -> str:
    """Format a chunk of turns into a readable conversation for Haiku."""
    lines = []
    for turn in chunk:
        role = "George" if turn["role"] == "user" else "Tam"
        lines.append(f"[{role}]: {turn['content'][:500]}")
    return "\n\n".join(lines)


# ── Discord Log Parsing ─────────────────────────────────────────────────────


def parse_discord_log(path: Path) -> list[dict]:
    """Parse Discord bot log into conversation turns."""
    turns = []
    if not path.exists():
        return turns

    with open(path) as f:
        for line in f:
            # User messages: "Message from _hankscorpio: content..."
            match = re.search(
                r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?Message from \w+: (.+)',
                line,
            )
            if match:
                content = match.group(2).strip().rstrip('...')
                if len(content) > 10:  # skip trivial messages
                    turns.append({
                        "role": "user",
                        "content": content,
                        "timestamp": match.group(1),
                    })
    return turns


# ── LLM Extraction ──────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """You are extracting structured knowledge from a conversation between George (a software engineer) and Tam (his AI assistant).

Given this conversation segment, extract:

1. **Facts**: Decisions made, preferences stated, concepts discussed, observations noted. Each fact should be a clear, standalone statement.
2. **Entities**: People, projects, systems, tools, or concepts mentioned.
3. **Relationships**: How entities connect to each other or to facts.

Valid fact types: decision, concept, preference, observation
Valid relation types: evokes, informs, depends_on, contradicts, supersedes, involves, uses, owns

Output ONLY valid JSON (no markdown, no explanation):
{
  "facts": [
    {"content": "...", "type": "decision|concept|preference|observation", "entities": ["entity1", "entity2"]}
  ],
  "relationships": [
    {"source": "entity or fact description", "target": "entity or fact description", "relation": "relation_type"}
  ]
}

If the conversation is trivial (greetings, acknowledgments, tool output), return: {"facts": [], "relationships": []}

Conversation segment:
"""


def _get_api_key() -> str:
    """Get Anthropic API key from Claude CLI OAuth credentials."""
    creds_path = Path.home() / ".claude" / ".credentials.json"
    with open(creds_path) as f:
        creds = json.load(f)
    return creds["claudeAiOauth"]["accessToken"]


_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=_get_api_key())
    return _client


def extract_with_haiku(chunk_text: str) -> dict | None:
    """Call Haiku via Anthropic SDK to extract structured knowledge."""
    try:
        client = get_client()
        message = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": EXTRACTION_PROMPT + chunk_text,
            }],
        )

        response_text = message.content[0].text

        # Strip markdown code fences if present
        response_text = re.sub(r'^```(?:json)?\s*', '', response_text, flags=re.MULTILINE)
        response_text = re.sub(r'```\s*$', '', response_text, flags=re.MULTILINE)
        response_text = response_text.strip()

        # Try to fix common JSON issues: trailing commas, truncation
        # Remove trailing commas before } or ]
        response_text = re.sub(r',\s*([}\]])', r'\1', response_text)

        try:
            return json.loads(response_text)
        except json.JSONDecodeError:
            # Try to salvage truncated JSON by closing open structures
            fixed = response_text
            # Count unclosed braces/brackets
            opens = fixed.count('{') - fixed.count('}')
            open_brackets = fixed.count('[') - fixed.count(']')
            # Try truncating to last complete entry and closing
            if opens > 0 or open_brackets > 0:
                # Find last complete fact entry
                last_brace = fixed.rfind('}')
                if last_brace > 0:
                    fixed = fixed[:last_brace + 1]
                    fixed += ']' * max(0, fixed.count('[') - fixed.count(']'))
                    fixed += '}' * max(0, fixed.count('{') - fixed.count('}'))
                    return json.loads(fixed)
            raise

    except json.JSONDecodeError as e:
        log.warning(f"Failed to parse Haiku response: {e}")
        return None
    except Exception as e:
        log.warning(f"Extraction error: {e}")
        return None


# ── Ingestion ────────────────────────────────────────────────────────────────


def ingest_extraction(conn: sqlite3.Connection, extraction: dict,
                      source: str, source_timestamp: str | None = None) -> int:
    """Store extracted facts and relationships in the database."""
    fact_count = 0
    fact_ids = {}  # content -> fact_id mapping for relationship resolution

    for fact in extraction.get("facts", []):
        content = fact.get("content", "").strip()
        fact_type = fact.get("type", "observation")
        if not content or len(content) < 10:
            continue

        # Dedup check
        existing = find_similar_fact(conn, content)
        if existing:
            fact_ids[content] = existing
            continue

        fact_id = store_fact(conn, content, fact_type, source, source_timestamp)
        fact_ids[content] = fact_id
        fact_count += 1

        # Link to entities
        for entity_name in fact.get("entities", []):
            if entity_name and len(entity_name) > 1:
                entity_id = find_or_create_entity(conn, entity_name, source)
                store_edge(conn, fact_id, entity_id, "mentions")

    # Create relationships
    for rel in extraction.get("relationships", []):
        src_name = rel.get("source", "")
        tgt_name = rel.get("target", "")
        rel_type = rel.get("relation", "related")

        # Try to resolve to existing fact/entity IDs
        src_id = fact_ids.get(src_name) or find_or_create_entity(conn, src_name, source)
        tgt_id = fact_ids.get(tgt_name) or find_or_create_entity(conn, tgt_name, source)

        if src_id and tgt_id and src_id != tgt_id:
            store_edge(conn, src_id, tgt_id, rel_type)

    return fact_count


def ingest_session(conn: sqlite3.Connection, session_id: str,
                   dry_run: bool = False) -> int:
    """Ingest a single Claude Code session."""
    # Check if already ingested
    row = conn.execute(
        "SELECT ingested_at FROM sessions_ingested WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row:
        log.info(f"Session {session_id[:8]}... already ingested at {row['ingested_at']}")
        return 0

    jsonl_path = SESSION_DIR / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        log.warning(f"Session file not found: {jsonl_path}")
        return 0

    turns = parse_session_jsonl(jsonl_path)
    if len(turns) < 2:
        log.info(f"Session {session_id[:8]}... too short ({len(turns)} turns), skipping")
        # Mark as ingested anyway to avoid re-processing
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO sessions_ingested(session_id, source_type, ingested_at, fact_count) VALUES (?, ?, ?, ?)",
            (session_id, "claude_code", now, 0),
        )
        conn.commit()
        return 0

    chunks = chunk_turns(turns)
    log.info(f"Session {session_id[:8]}...: {len(turns)} turns, {len(chunks)} chunks")

    total_facts = 0
    first_timestamp = turns[0].get("timestamp", "")

    for i, chunk in enumerate(chunks):
        chunk_text = format_chunk_for_extraction(chunk)

        if dry_run:
            log.info(f"  Chunk {i+1}/{len(chunks)}: {len(chunk)} turns, {len(chunk_text)} chars")
            log.info(f"  Preview: {chunk_text[:200]}...")
            continue

        extraction = extract_with_haiku(chunk_text)
        if extraction:
            facts_stored = ingest_extraction(
                conn, extraction, source=session_id,
                source_timestamp=chunk[0].get("timestamp"),
            )
            total_facts += facts_stored
            log.info(f"  Chunk {i+1}/{len(chunks)}: {facts_stored} facts extracted")
        else:
            log.warning(f"  Chunk {i+1}/{len(chunks)}: extraction failed")

    if not dry_run:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO sessions_ingested(session_id, source_type, ingested_at, fact_count) VALUES (?, ?, ?, ?)",
            (session_id, "claude_code", now, total_facts),
        )
        conn.commit()

    log.info(f"Session {session_id[:8]}...: {total_facts} facts ingested")
    return total_facts


def ingest_discord(conn: sqlite3.Connection, dry_run: bool = False) -> int:
    """Ingest Discord logs."""
    if not DISCORD_LOG.exists():
        log.warning(f"Discord log not found: {DISCORD_LOG}")
        return 0

    # Check if already ingested
    row = conn.execute(
        "SELECT ingested_at FROM sessions_ingested WHERE session_id = 'discord_log'",
    ).fetchone()
    if row:
        log.info(f"Discord log already ingested at {row['ingested_at']}")
        return 0

    turns = parse_discord_log(DISCORD_LOG)
    if not turns:
        log.info("No parseable Discord turns found")
        return 0

    chunks = chunk_turns(turns)
    log.info(f"Discord log: {len(turns)} turns, {len(chunks)} chunks")

    total_facts = 0
    for i, chunk in enumerate(chunks):
        chunk_text = format_chunk_for_extraction(chunk)

        if dry_run:
            log.info(f"  Chunk {i+1}/{len(chunks)}: {len(chunk)} turns")
            continue

        extraction = extract_with_haiku(chunk_text)
        if extraction:
            facts_stored = ingest_extraction(conn, extraction, source="discord")
            total_facts += facts_stored
            log.info(f"  Chunk {i+1}/{len(chunks)}: {facts_stored} facts")

    if not dry_run:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO sessions_ingested(session_id, source_type, ingested_at, fact_count) VALUES (?, ?, ?, ?)",
            ("discord_log", "discord", now, total_facts),
        )
        conn.commit()

    return total_facts


def seed_memory_files(conn: sqlite3.Connection) -> int:
    """Import existing .md memory files as seed facts."""
    if not MEMORY_DIR.exists():
        log.warning(f"Memory dir not found: {MEMORY_DIR}")
        return 0

    total = 0
    for md_file in MEMORY_DIR.glob("*.md"):
        if md_file.name == "MEMORY.md":
            continue  # skip the index

        text = md_file.read_text(errors="replace")

        # Parse frontmatter
        fact_type = "observation"
        if text.startswith("---"):
            try:
                end = text.index("---", 3)
                frontmatter = text[3:end]
                body = text[end + 3:].strip()
                for line in frontmatter.splitlines():
                    if line.startswith("type:"):
                        fm_type = line.split(":", 1)[1].strip()
                        type_map = {
                            "user": "observation",
                            "feedback": "preference",
                            "project": "concept",
                            "reference": "observation",
                        }
                        fact_type = type_map.get(fm_type, "observation")
            except ValueError:
                body = text
        else:
            body = text

        if not body or len(body) < 10:
            continue

        # Check for duplicate
        existing = find_similar_fact(conn, body[:500])
        if existing:
            log.info(f"  {md_file.name}: duplicate, skipping")
            continue

        fact_id = store_fact(conn, body[:2000], fact_type, source=f"memory:{md_file.name}")
        total += 1
        log.info(f"  {md_file.name}: stored as {fact_type} (id={fact_id})")

    log.info(f"Seeded {total} facts from memory files")
    return total


# ── Entry point ──────────────────────────────────────────────────────────────


def get_all_session_ids() -> list[str]:
    """Get all session IDs from the JSONL directory."""
    ids = []
    for f in SESSION_DIR.glob("*.jsonl"):
        ids.append(f.stem)
    return sorted(ids)


def main():
    parser = argparse.ArgumentParser(description="Ingest session transcripts into tam-memory")
    parser.add_argument("--session", help="Ingest a specific session by UUID")
    parser.add_argument("--discord", action="store_true", help="Ingest Discord logs")
    parser.add_argument("--seed-memory", action="store_true", help="Import existing memory .md files")
    parser.add_argument("--dry-run", action="store_true", help="Preview without storing")
    parser.add_argument("--limit", type=int, help="Limit number of sessions to process")
    args = parser.parse_args()

    conn = open_db()

    if args.seed_memory:
        seed_memory_files(conn)
        return

    if args.discord:
        ingest_discord(conn, dry_run=args.dry_run)
        return

    if args.session:
        ingest_session(conn, args.session, dry_run=args.dry_run)
        return

    # Default: ingest all new sessions
    session_ids = get_all_session_ids()
    log.info(f"Found {len(session_ids)} sessions")

    # Filter already ingested
    ingested = set()
    for row in conn.execute("SELECT session_id FROM sessions_ingested"):
        ingested.add(row["session_id"])
    new_sessions = [s for s in session_ids if s not in ingested]
    log.info(f"{len(new_sessions)} new sessions to ingest")

    if args.limit:
        new_sessions = new_sessions[:args.limit]

    total_facts = 0
    for i, session_id in enumerate(new_sessions):
        log.info(f"\n[{i+1}/{len(new_sessions)}] Processing {session_id[:8]}...")
        facts = ingest_session(conn, session_id, dry_run=args.dry_run)
        total_facts += facts

    log.info(f"\nDone. Total facts ingested: {total_facts}")


if __name__ == "__main__":
    main()
