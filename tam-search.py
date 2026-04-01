#!/usr/bin/env python3
"""
tam-search.py — Vault search for Tam

Combines BM25 (FTS5) and vector similarity search, merges with Reciprocal
Rank Fusion, and returns ranked results.

Usage:
    python3 tam-search.py "your query here"
    python3 tam-search.py "query" --vault tam       # restrict to one vault
    python3 tam-search.py "query" --top 5           # number of results
    python3 tam-search.py "query" --json            # machine-readable output
"""

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path

import sqlite_vec

# ── Config ───────────────────────────────────────────────────────────────────

TAM_HOME = Path(os.environ.get("TAM_HOME", Path.home() / "tam"))
DB_PATH = TAM_HOME / "vault_index.db"
EMBED_MODEL = "nomic-embed-text"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
VAULTS_ROOT = Path("/home/aldric/vaults")

# RRF constant — 60 is the standard default
RRF_K = 60

# ── DB ────────────────────────────────────────────────────────────────────────

def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.row_factory = sqlite3.Row
    return conn

# ── Embedding ─────────────────────────────────────────────────────────────────

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

# ── Search ────────────────────────────────────────────────────────────────────

def bm25_search(conn: sqlite3.Connection, query: str, vault: str | None, top_k: int) -> list[dict]:
    sql = """
        SELECT d.id, d.path, d.vault, d.title,
               snippet(docs_fts, 2, '[', ']', '...', 20) AS snippet,
               rank AS score
        FROM docs_fts
        JOIN docs d ON d.id = docs_fts.rowid
        WHERE docs_fts MATCH ?
    """
    params: list = [query]
    if vault:
        sql += " AND d.vault = ?"
        params.append(vault)
    sql += " ORDER BY rank LIMIT ?"
    params.append(top_k)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def vec_search(conn: sqlite3.Connection, query_vec: list[float], vault: str | None, top_k: int) -> list[dict]:
    sql = """
        SELECT d.id, d.path, d.vault, d.title,
               substr(d.body, 1, 300) AS snippet,
               v.distance AS score
        FROM docs_vec v
        JOIN docs d ON d.id = v.rowid
        WHERE v.embedding MATCH ?
          AND k = ?
    """
    params: list = [sqlite_vec.serialize_float32(query_vec), top_k * 2]
    if vault:
        sql += " AND d.vault = ?"
        params.append(vault)
    sql += " ORDER BY v.distance"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def rrf_merge(bm25_results: list[dict], vec_results: list[dict], top_k: int) -> list[dict]:
    """Reciprocal Rank Fusion of two ranked lists."""
    scores: dict[int, float] = {}
    meta: dict[int, dict] = {}

    for rank, doc in enumerate(bm25_results):
        doc_id = doc["id"]
        scores[doc_id] = scores.get(doc_id, 0) + 1 / (RRF_K + rank + 1)
        meta[doc_id] = doc

    for rank, doc in enumerate(vec_results):
        doc_id = doc["id"]
        scores[doc_id] = scores.get(doc_id, 0) + 1 / (RRF_K + rank + 1)
        if doc_id not in meta:
            meta[doc_id] = doc

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
    results = []
    for doc_id, rrf_score in ranked:
        entry = dict(meta[doc_id])
        entry["rrf_score"] = round(rrf_score, 6)
        results.append(entry)
    return results


def search(query: str, vault: str | None = None, top_k: int = 5) -> list[dict]:
    conn = open_db()

    # Check index has content
    count = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    if count == 0:
        return []

    # BM25
    try:
        bm25 = bm25_search(conn, query, vault, top_k * 2)
    except Exception:
        bm25 = []

    # Vector
    try:
        q_vec = embed(query)
        vec = vec_search(conn, q_vec, vault, top_k * 2)
    except Exception:
        vec = []

    if not bm25 and not vec:
        return []

    return rrf_merge(bm25, vec, top_k)

# ── Formatting ────────────────────────────────────────────────────────────────

def format_results(results: list[dict], as_json: bool) -> str:
    if as_json:
        return json.dumps(results, indent=2)

    if not results:
        return "No results found."

    lines = []
    for i, r in enumerate(results, 1):
        try:
            rel = Path(r["path"]).relative_to(VAULTS_ROOT)
        except ValueError:
            rel = Path(r["path"])
        lines.append(f"{i}. [{r['vault']}] {r['title']}")
        lines.append(f"   {rel}")
        if r.get("snippet"):
            snippet = r["snippet"].replace("\n", " ").strip()
            lines.append(f"   {snippet[:200]}")
        lines.append(f"   RRF score: {r.get('rrf_score', '?')}")
        lines.append("")
    return "\n".join(lines).strip()

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Search Tam's vaults")
    parser.add_argument("query", nargs="+", help="Search query")
    parser.add_argument("--vault", choices=["george", "tam"], help="Restrict to one vault")
    parser.add_argument("--top", type=int, default=5, help="Number of results (default: 5)")
    parser.add_argument("--json", action="store_true", dest="as_json", help="JSON output")
    args = parser.parse_args()

    query = " ".join(args.query)
    results = search(query, vault=args.vault, top_k=args.top)
    print(format_results(results, args.as_json))


if __name__ == "__main__":
    main()
