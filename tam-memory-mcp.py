#!/usr/bin/env python3
"""
tam-memory-mcp.py — Unified memory MCP server for Tam

Provides hybrid search (BM25 + vector + graph boost), persistent fact storage,
and a knowledge graph over a single SQLite database.

Tools: recall, remember, graph_traverse, list_entities, ingest_session
Resources: recent_context
"""

import json
import logging
import os
import re
import sqlite3
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import networkx as nx
import sqlite_vec
from mcp.server.fastmcp import FastMCP

# ── Config ───────────────────────────────────────────────────────────────────

TAM_HOME = Path(os.environ.get("TAM_HOME", Path.home() / "tam"))
DB_PATH = TAM_HOME / "tam_memory.db"
EMBED_MODEL = "nomic-embed-text"
EMBED_DIM = 768
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
RRF_K = 60
GRAPH_BOOST_FACTOR = 0.3  # extra RRF score for graph-connected results
DECAY_HALF_LIFE_DAYS = 30  # how fast decay_rank falls off
RECENCY_BOOST_WEIGHT = 0.02  # max additive boost for very recent facts
RECENCY_WINDOW_DAYS = 14  # facts older than this get no recency boost

SESSION_JSONL_DIR = Path.home() / ".claude" / "projects" / "-home-aldric-tam"

# ── Logging ──────────────────────────────────────────────────────────────────

(TAM_HOME / "logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(TAM_HOME / "logs" / "memory-mcp.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("tam-memory")

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


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            type TEXT NOT NULL,
            source TEXT,
            source_timestamp TEXT,
            created_at TEXT NOT NULL,
            decay_rank REAL DEFAULT 1.0
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
            content, type,
            content=facts, content_rowid=id
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS facts_vec USING vec0(
            embedding FLOAT[{EMBED_DIM}]
        );

        CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
            INSERT INTO facts_fts(rowid, content, type)
            VALUES (new.id, new.content, new.type);
        END;

        CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
            INSERT INTO facts_fts(facts_fts, rowid, content, type)
            VALUES ('delete', old.id, old.content, old.type);
        END;

        CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
            INSERT INTO facts_fts(facts_fts, rowid, content, type)
            VALUES ('delete', old.id, old.content, old.type);
            INSERT INTO facts_fts(rowid, content, type)
            VALUES (new.id, new.content, new.type);
        END;

        CREATE TABLE IF NOT EXISTS edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
            target_id INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
            relation TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
        CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
        CREATE INDEX IF NOT EXISTS idx_edges_relation ON edges(relation);

        CREATE TABLE IF NOT EXISTS sessions_ingested (
            session_id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            ingested_at TEXT NOT NULL,
            fact_count INTEGER DEFAULT 0
        );
    """)
    conn.commit()


# ── Embedding ────────────────────────────────────────────────────────────────


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


# ── Graph ────────────────────────────────────────────────────────────────────

_graph: nx.DiGraph | None = None


def load_graph(conn: sqlite3.Connection) -> nx.DiGraph:
    global _graph
    g = nx.DiGraph()
    # Add all facts as nodes
    for row in conn.execute("SELECT id, content, type FROM facts"):
        g.add_node(row["id"], content=row["content"], type=row["type"])
    # Add all edges
    for row in conn.execute("SELECT source_id, target_id, relation, weight FROM edges"):
        g.add_edge(row["source_id"], row["target_id"],
                    relation=row["relation"], weight=row["weight"])
    _graph = g
    return g


def get_graph(conn: sqlite3.Connection) -> nx.DiGraph:
    global _graph
    if _graph is None:
        return load_graph(conn)
    return _graph


def rebuild_graph(conn: sqlite3.Connection) -> nx.DiGraph:
    global _graph
    _graph = None
    return load_graph(conn)


# ── Search ───────────────────────────────────────────────────────────────────


# ── Temporal detection ───────────────────────────────────────────────────────

# Maps temporal phrases to approximate day ranges (lookback_days, window_days)
TEMPORAL_PATTERNS: list[tuple[str, int, int]] = [
    (r"\byesterday\b", 1, 2),
    (r"\btoday\b", 0, 1),
    (r"\blast\s+(?:few\s+)?hours?\b", 0, 1),
    (r"\bthis\s+morning\b", 0, 1),
    (r"\bthis\s+week\b", 0, 7),
    (r"\blast\s+week\b", 0, 10),       # "last week" = ~past 10 days (covers the week that just ended)
    (r"\brecently\b", 0, 7),
    (r"\blast\s+(?:few\s+)?days?\b", 0, 5),
    (r"\blast\s+month\b", 14, 45),     # generous window for "last month"
    (r"\bthis\s+month\b", 0, 30),
    (r"\blast\s+(?:couple|two|2)\s+weeks?\b", 0, 16),
]


def detect_temporal_range(query: str) -> tuple[datetime, datetime] | None:
    """Detect temporal intent in a query. Returns (earliest, latest) or None."""
    q = query.lower()
    now = datetime.now(timezone.utc)

    for pattern, start_days, end_days in TEMPORAL_PATTERNS:
        if re.search(pattern, q):
            earliest = now - timedelta(days=end_days)
            latest = now - timedelta(days=start_days)
            return (earliest, latest)
    return None


def recency_score(source_timestamp: str | None, now: datetime | None = None) -> float:
    """Compute a recency boost between 0 and RECENCY_BOOST_WEIGHT.

    Linear decay: facts from right now get full boost, facts older than
    RECENCY_WINDOW_DAYS get 0.
    """
    if not source_timestamp:
        return 0.0
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        ts = datetime.fromisoformat(source_timestamp.replace("Z", "+00:00"))
        age_days = (now - ts).total_seconds() / 86400
        if age_days < 0:
            age_days = 0
        factor = max(0.0, 1.0 - age_days / RECENCY_WINDOW_DAYS)
        return RECENCY_BOOST_WEIGHT * factor
    except (ValueError, TypeError):
        return 0.0


def bm25_search(conn: sqlite3.Connection, query: str, top_k: int,
                type_filter: str | None = None) -> list[dict]:
    sql = """
        SELECT f.id, f.content, f.type, f.source, f.source_timestamp,
               f.created_at, f.decay_rank,
               snippet(facts_fts, 0, '[', ']', '...', 30) AS snippet,
               rank AS score
        FROM facts_fts
        JOIN facts f ON f.id = facts_fts.rowid
        WHERE facts_fts MATCH ?
    """
    params: list = [query]
    if type_filter:
        sql += " AND f.type = ?"
        params.append(type_filter)
    sql += " ORDER BY rank LIMIT ?"
    params.append(top_k)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def vec_search(conn: sqlite3.Connection, query_vec: list[float], top_k: int,
               type_filter: str | None = None) -> list[dict]:
    sql = """
        SELECT f.id, f.content, f.type, f.source, f.source_timestamp,
               f.created_at, f.decay_rank,
               substr(f.content, 1, 200) AS snippet,
               v.distance AS score
        FROM facts_vec v
        JOIN facts f ON f.id = v.rowid
        WHERE v.embedding MATCH ?
          AND k = ?
    """
    params: list = [sqlite_vec.serialize_float32(query_vec), top_k * 2]
    if type_filter:
        sql += " AND f.type = ?"
        params.append(type_filter)
    sql += " ORDER BY v.distance"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def temporal_search(conn: sqlite3.Connection, earliest: datetime,
                    latest: datetime, top_k: int,
                    type_filter: str | None = None) -> list[dict]:
    """Fetch facts within a time range, prioritizing substantive types."""
    # Prefer decisions/observations/concepts over bare entities
    type_order = "CASE f.type WHEN 'decision' THEN 1 WHEN 'observation' THEN 2 WHEN 'concept' THEN 3 WHEN 'preference' THEN 4 ELSE 5 END"
    sql = f"""
        SELECT f.id, f.content, f.type, f.source, f.source_timestamp,
               f.created_at, f.decay_rank,
               substr(f.content, 1, 200) AS snippet,
               0.0 AS score
        FROM facts f
        WHERE f.source_timestamp >= ? AND f.source_timestamp <= ?
          AND length(f.content) > 20
    """
    params: list = [earliest.isoformat(), latest.isoformat()]
    if type_filter:
        sql += " AND f.type = ?"
        params.append(type_filter)
    sql += f" ORDER BY {type_order}, f.source_timestamp DESC LIMIT ?"
    params.append(top_k)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def rrf_merge(*result_lists: list[dict], top_k: int) -> list[dict]:
    scores: dict[int, float] = {}
    meta: dict[int, dict] = {}

    for result_list in result_lists:
        for rank, doc in enumerate(result_list):
            doc_id = doc["id"]
            scores[doc_id] = scores.get(doc_id, 0) + 1 / (RRF_K + rank + 1)
            if doc_id not in meta:
                meta[doc_id] = doc

    # Apply decay_rank as a multiplier + recency boost
    now = datetime.now(timezone.utc)
    for doc_id in scores:
        decay = meta[doc_id].get("decay_rank", 1.0) or 1.0
        scores[doc_id] *= decay
        scores[doc_id] += recency_score(meta[doc_id].get("source_timestamp"), now)

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
    results = []
    for doc_id, rrf_score in ranked:
        entry = dict(meta[doc_id])
        entry["rrf_score"] = round(rrf_score, 6)
        results.append(entry)
    return results


def graph_boost(results: list[dict], query: str,
                conn: sqlite3.Connection) -> list[dict]:
    """Boost results that are graph-connected to other top results."""
    if len(results) < 2:
        return results

    g = get_graph(conn)
    result_ids = {r["id"] for r in results}

    for r in results:
        rid = r["id"]
        if rid not in g:
            continue
        # Count connections to other results
        neighbors = set(g.successors(rid)) | set(g.predecessors(rid))
        connected = neighbors & result_ids
        if connected:
            r["rrf_score"] = r.get("rrf_score", 0) + GRAPH_BOOST_FACTOR * len(connected)
            r["graph_connections"] = len(connected)

    results.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)
    return results


# ── Fact storage ─────────────────────────────────────────────────────────────


def store_fact(conn: sqlite3.Connection, content: str, fact_type: str,
               source: str = "manual",
               source_timestamp: str | None = None) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO facts(content, type, source, source_timestamp, created_at, decay_rank)
           VALUES (?, ?, ?, ?, ?, 1.0)""",
        (content, fact_type, source, source_timestamp or now, now),
    )
    fact_id = cur.lastrowid

    # Embed and store vector
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
               relation: str, weight: float = 1.0) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO edges(source_id, target_id, relation, weight, created_at) VALUES (?, ?, ?, ?, ?)",
        (source_id, target_id, relation, weight, now),
    )
    conn.commit()
    # Rebuild graph with new edge
    rebuild_graph(conn)
    return cur.lastrowid


def find_fact_by_content(conn: sqlite3.Connection, content: str,
                         threshold: float = 0.9) -> int | None:
    """Find an existing fact by embedding similarity. Returns fact ID or None."""
    try:
        vec = embed(content)
    except Exception:
        return None

    row = conn.execute(
        """SELECT f.id, v.distance FROM facts_vec v
           JOIN facts f ON f.id = v.rowid
           WHERE v.embedding MATCH ? AND k = 1""",
        (sqlite_vec.serialize_float32(vec),),
    ).fetchone()

    if row and row["distance"] < (1.0 - threshold):
        return row["id"]
    return None


# ── MCP Server ───────────────────────────────────────────────────────────────

mcp = FastMCP(
    "tam-memory",
    instructions=(
        "Tam's persistent memory system. Use 'recall' to search memories, "
        "'remember' to store new facts, 'graph_traverse' to explore connections, "
        "and 'list_entities' to see known entities."
    ),
)

# Initialize DB on import
_conn = open_db()
init_db(_conn)
log.info(f"Memory DB initialized at {DB_PATH}")


@mcp.tool()
def recall(query: str, top_k: int = 5, type_filter: str | None = None) -> str:
    """Search Tam's memory using hybrid BM25 + vector + graph-boosted retrieval.

    Args:
        query: Natural language search query
        top_k: Number of results to return (default 5)
        type_filter: Optional filter by fact type (entity, decision, concept, preference, observation)
    """
    conn = open_db()

    # Detect temporal intent
    time_range = detect_temporal_range(query)

    # BM25
    try:
        bm25 = bm25_search(conn, query, top_k * 2, type_filter)
    except Exception:
        bm25 = []

    # Vector
    try:
        q_vec = embed(query)
        vec = vec_search(conn, q_vec, top_k * 2, type_filter)
    except Exception:
        vec = []

    # Third retrieval path: temporal search when time intent detected
    temporal = []
    if time_range:
        earliest, latest = time_range
        temporal = temporal_search(conn, earliest, latest, top_k * 2, type_filter)

    if not bm25 and not vec and not temporal:
        return json.dumps({"results": [], "message": "No matching memories found."})

    # Merge all retrieval paths via RRF
    result_lists = [r for r in [bm25, vec, temporal] if r]
    merged = rrf_merge(*result_lists, top_k=top_k * 2)

    boosted = graph_boost(merged, query, conn)[:top_k]

    # Enrich with graph neighbors
    g = get_graph(conn)
    output = []
    for r in boosted:
        entry = {
            "id": r["id"],
            "content": r["content"],
            "type": r["type"],
            "source": r["source"],
            "source_timestamp": r["source_timestamp"],
            "rrf_score": r.get("rrf_score"),
            "graph_connections": r.get("graph_connections", 0),
        }
        # Add immediate graph neighbors
        rid = r["id"]
        if rid in g:
            neighbors = []
            for succ in g.successors(rid):
                edge_data = g.edges[rid, succ]
                if succ in g.nodes:
                    neighbors.append({
                        "id": succ,
                        "relation": edge_data.get("relation", "related"),
                        "content": g.nodes[succ].get("content", "")[:100],
                    })
            for pred in g.predecessors(rid):
                edge_data = g.edges[pred, rid]
                if pred in g.nodes:
                    neighbors.append({
                        "id": pred,
                        "relation": f"←{edge_data.get('relation', 'related')}",
                        "content": g.nodes[pred].get("content", "")[:100],
                    })
            if neighbors:
                entry["neighbors"] = neighbors[:5]
        output.append(entry)

    response = {"results": output}
    if time_range:
        response["temporal_filter"] = {
            "earliest": time_range[0].isoformat(),
            "latest": time_range[1].isoformat(),
        }
    return json.dumps(response, indent=2)


@mcp.tool()
def remember(content: str, type: str,
             entities: list[str] | None = None,
             relations: list[dict] | None = None) -> str:
    """Store a new fact in Tam's memory with optional entity links.

    Args:
        content: The fact, decision, concept, or observation to remember
        type: One of: entity, decision, concept, preference, observation
        entities: Optional list of entity names to link this fact to (creates edges)
        relations: Optional list of {source, target, relation} dicts to create edges
    """
    conn = open_db()

    # Check for duplicates
    existing = find_fact_by_content(conn, content)
    if existing:
        return json.dumps({
            "status": "duplicate",
            "existing_id": existing,
            "message": "A very similar fact already exists.",
        })

    fact_id = store_fact(conn, content, type)
    edges_created = 0

    # Link to existing entity facts by name
    if entities:
        for entity_name in entities:
            row = conn.execute(
                "SELECT id FROM facts WHERE type = 'entity' AND content LIKE ? LIMIT 1",
                (f"%{entity_name}%",),
            ).fetchone()
            if row:
                store_edge(conn, fact_id, row["id"], "mentions")
                edges_created += 1
            else:
                # Create the entity if it doesn't exist
                entity_id = store_fact(conn, entity_name, "entity")
                store_edge(conn, fact_id, entity_id, "mentions")
                edges_created += 1

    # Create explicit relations
    if relations:
        for rel in relations:
            src = rel.get("source_id") or fact_id
            tgt = rel.get("target_id")
            rel_type = rel.get("relation", "related")
            if tgt:
                store_edge(conn, src, tgt, rel_type)
                edges_created += 1

    rebuild_graph(conn)

    return json.dumps({
        "status": "stored",
        "fact_id": fact_id,
        "edges_created": edges_created,
    })


@mcp.tool()
def graph_traverse(entity: str, depth: int = 2,
                   rel_type: str | None = None) -> str:
    """Walk the knowledge graph from an entity, following relationships.

    Args:
        entity: Name or content fragment to find the starting node
        depth: How many hops to traverse (default 2)
        rel_type: Optional filter to only follow edges of this relation type
    """
    conn = open_db()
    g = get_graph(conn)

    MAX_NODES = 50  # cap to prevent massive results

    # Find starting node(s) by content match — prefer exact entity type
    start_nodes = []
    for node_id, data in g.nodes(data=True):
        if entity.lower() in data.get("content", "").lower():
            start_nodes.append(node_id)
    # Limit starting nodes to avoid fan-out from broad matches
    start_nodes = start_nodes[:5]

    if not start_nodes:
        return json.dumps({"error": f"No entity matching '{entity}' found in graph."})

    # BFS traversal
    visited = set()
    result_nodes = []
    result_edges = []
    frontier = [(nid, 0) for nid in start_nodes]

    while frontier:
        if len(result_nodes) >= MAX_NODES:
            break
        node_id, d = frontier.pop(0)
        if node_id in visited or d > depth:
            continue
        visited.add(node_id)

        node_data = g.nodes.get(node_id, {})
        result_nodes.append({
            "id": node_id,
            "content": node_data.get("content", ""),
            "type": node_data.get("type", ""),
            "depth": d,
        })

        # Follow outgoing edges
        for succ in g.successors(node_id):
            edge_data = g.edges[node_id, succ]
            rel = edge_data.get("relation", "related")
            if rel_type and rel != rel_type:
                continue
            result_edges.append({
                "from": node_id, "to": succ,
                "relation": rel, "weight": edge_data.get("weight", 1.0),
            })
            if succ not in visited:
                frontier.append((succ, d + 1))

        # Follow incoming edges
        for pred in g.predecessors(node_id):
            edge_data = g.edges[pred, node_id]
            rel = edge_data.get("relation", "related")
            if rel_type and rel != rel_type:
                continue
            result_edges.append({
                "from": pred, "to": node_id,
                "relation": rel, "weight": edge_data.get("weight", 1.0),
            })
            if pred not in visited:
                frontier.append((pred, d + 1))

    return json.dumps({
        "start": entity,
        "nodes": result_nodes,
        "edges": result_edges,
        "node_count": len(result_nodes),
        "edge_count": len(result_edges),
    }, indent=2)


@mcp.tool()
def list_entities(type_filter: str | None = None) -> str:
    """List all known entities/facts in the knowledge graph.

    Args:
        type_filter: Optional filter by type (entity, decision, concept, preference, observation)
    """
    conn = open_db()
    sql = "SELECT id, content, type, created_at FROM facts"
    params = []
    if type_filter:
        sql += " WHERE type = ?"
        params.append(type_filter)
    sql += " ORDER BY created_at DESC LIMIT 100"
    rows = conn.execute(sql, params).fetchall()

    entities = [{"id": r["id"], "content": r["content"], "type": r["type"],
                 "created_at": r["created_at"]} for r in rows]

    # Add edge counts
    g = get_graph(conn)
    for e in entities:
        nid = e["id"]
        if nid in g:
            e["connections"] = g.degree(nid)
        else:
            e["connections"] = 0

    return json.dumps({"entities": entities, "total": len(entities)}, indent=2)


@mcp.tool()
def ingest_session(session_id: str) -> str:
    """Ingest a Claude Code session JSONL file, extracting facts and relationships.

    This is a placeholder — full extraction requires the tam-ingest.py pipeline
    with Haiku. For now, it extracts user messages as raw observations.

    Args:
        session_id: The UUID of the session to ingest
    """
    conn = open_db()

    # Check if already ingested
    row = conn.execute(
        "SELECT ingested_at FROM sessions_ingested WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row:
        return json.dumps({"status": "already_ingested", "ingested_at": row["ingested_at"]})

    # Find JSONL file
    jsonl_path = SESSION_JSONL_DIR / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        return json.dumps({"error": f"Session file not found: {jsonl_path}"})

    # Basic extraction: pull user messages
    fact_count = 0
    with open(jsonl_path) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("type") != "user":
                continue

            msg = entry.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 20:
                timestamp = entry.get("timestamp", "")
                store_fact(conn, content, "observation",
                           source=session_id, source_timestamp=timestamp)
                fact_count += 1

    # Mark as ingested
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO sessions_ingested(session_id, source_type, ingested_at, fact_count) VALUES (?, ?, ?, ?)",
        (session_id, "claude_code", now, fact_count),
    )
    conn.commit()
    rebuild_graph(conn)

    return json.dumps({
        "status": "ingested",
        "session_id": session_id,
        "facts_extracted": fact_count,
        "message": "Basic ingestion complete. Run tam-ingest.py for full LLM extraction.",
    })


# ── Resources ────────────────────────────────────────────────────────────────


@mcp.resource("memory://recent_context")
def recent_context() -> str:
    """Recent facts, decisions, and activity from the last 48 hours."""
    conn = open_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

    rows = conn.execute(
        """SELECT id, content, type, source, source_timestamp, created_at
           FROM facts
           WHERE created_at > ?
           ORDER BY created_at DESC
           LIMIT 50""",
        (cutoff,),
    ).fetchall()

    facts = [dict(r) for r in rows]

    # Also include recent session ingestion status
    sessions = conn.execute(
        """SELECT session_id, source_type, ingested_at, fact_count
           FROM sessions_ingested
           WHERE ingested_at > ?
           ORDER BY ingested_at DESC
           LIMIT 10""",
        (cutoff,),
    ).fetchall()

    return json.dumps({
        "recent_facts": facts,
        "recent_sessions_ingested": [dict(s) for s in sessions],
        "as_of": datetime.now(timezone.utc).isoformat(),
    }, indent=2)


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("Starting tam-memory MCP server...")
    mcp.run(transport="stdio")
