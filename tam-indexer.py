#!/usr/bin/env python3
"""
tam-indexer.py — Vault indexer for Tam

Watches /home/aldric/vaults/george and /home/aldric/vaults/tam for markdown
file changes and keeps a sqlite-vec + FTS5 index up to date.

Usage:
    python3 tam-indexer.py               # watch mode (for systemd service)
    python3 tam-indexer.py --reindex     # full reindex then exit
    python3 tam-indexer.py --reindex --watch  # reindex then stay in watch mode
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

import sqlite_vec
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent, FileDeletedEvent, FileMovedEvent
from watchdog.observers import Observer

# ── Config ───────────────────────────────────────────────────────────────────

TAM_HOME = Path(os.environ.get("TAM_HOME", Path.home() / "tam"))
VAULTS_ROOT = Path("/home/aldric/vaults")
WATCH_PATHS = [VAULTS_ROOT / "george", VAULTS_ROOT / "tam"]
DB_PATH = TAM_HOME / "vault_index.db"
EMBED_MODEL = "nomic-embed-text"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_DIM = 768  # nomic-embed-text output dimension

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(TAM_HOME / "logs" / "indexer.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("tam-indexer")

# ── Database ─────────────────────────────────────────────────────────────────

def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS docs (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            path    TEXT UNIQUE NOT NULL,
            vault   TEXT NOT NULL,
            title   TEXT,
            body    TEXT NOT NULL,
            mtime   REAL NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            path UNINDEXED,
            title,
            body,
            content=docs,
            content_rowid=id
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS docs_vec USING vec0(
            embedding FLOAT[{EMBED_DIM}]
        );

        CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
            INSERT INTO docs_fts(rowid, path, title, body)
            VALUES (new.id, new.path, new.title, new.body);
        END;

        CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
            INSERT INTO docs_fts(docs_fts, rowid, path, title, body)
            VALUES ('delete', old.id, old.path, old.title, old.body);
        END;

        CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs BEGIN
            INSERT INTO docs_fts(docs_fts, rowid, path, title, body)
            VALUES ('delete', old.id, old.path, old.title, old.body);
            INSERT INTO docs_fts(rowid, path, title, body)
            VALUES (new.id, new.path, new.title, new.body);
        END;
    """)
    conn.commit()

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

# ── Document processing ──────────────────────────────────────────────────────

def extract(path: Path) -> tuple[str, str]:
    """Return (title, body) from a markdown file."""
    text = path.read_text(errors="replace")
    lines = text.splitlines()
    title = path.stem
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            break
    # Strip YAML frontmatter
    body_lines = lines
    if lines and lines[0].strip() == "---":
        try:
            end = lines.index("---", 1)
            body_lines = lines[end + 1:]
        except ValueError:
            pass
    body = "\n".join(body_lines).strip()
    return title, body


def vault_name(path: Path) -> str:
    try:
        rel = path.relative_to(VAULTS_ROOT)
        return rel.parts[0]
    except ValueError:
        return "unknown"


def upsert_file(conn: sqlite3.Connection, path: Path) -> None:
    if not path.exists() or path.suffix.lower() != ".md":
        return
    mtime = path.stat().st_mtime
    path_str = str(path)

    row = conn.execute("SELECT id, mtime FROM docs WHERE path = ?", (path_str,)).fetchone()
    if row and row[1] >= mtime:
        return  # Up to date

    title, body = extract(path)
    if not body:
        return

    embed_text = f"{title}\n\n{body}"[:8000]  # nomic context limit
    try:
        vector = embed(embed_text)
    except Exception as e:
        log.warning(f"Embed failed for {path}: {e}")
        return

    vault = vault_name(path)

    if row:
        doc_id = row[0]
        conn.execute(
            "UPDATE docs SET vault=?, title=?, body=?, mtime=? WHERE id=?",
            (vault, title, body, mtime, doc_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO docs_vec(rowid, embedding) VALUES (?, ?)",
            (doc_id, sqlite_vec.serialize_float32(vector)),
        )
        log.info(f"Updated: {path.relative_to(VAULTS_ROOT)}")
    else:
        cur = conn.execute(
            "INSERT INTO docs(path, vault, title, body, mtime) VALUES (?,?,?,?,?)",
            (path_str, vault, title, body, mtime),
        )
        doc_id = cur.lastrowid
        conn.execute(
            "INSERT INTO docs_vec(rowid, embedding) VALUES (?, ?)",
            (doc_id, sqlite_vec.serialize_float32(vector)),
        )
        log.info(f"Indexed: {path.relative_to(VAULTS_ROOT)}")

    conn.commit()


def delete_file(conn: sqlite3.Connection, path: Path) -> None:
    path_str = str(path)
    row = conn.execute("SELECT id FROM docs WHERE path = ?", (path_str,)).fetchone()
    if row:
        doc_id = row[0]
        conn.execute("DELETE FROM docs_vec WHERE rowid = ?", (doc_id,))
        conn.execute("DELETE FROM docs WHERE id = ?", (doc_id,))
        conn.commit()
        log.info(f"Removed: {path}")

# ── Full reindex ──────────────────────────────────────────────────────────────

def reindex(conn: sqlite3.Connection) -> None:
    log.info("Starting full reindex...")
    count = 0
    for vault_path in WATCH_PATHS:
        if not vault_path.exists():
            log.warning(f"Vault path not found: {vault_path}")
            continue
        for md in vault_path.rglob("*.md"):
            upsert_file(conn, md)
            count += 1
    log.info(f"Reindex complete. Processed {count} files.")

# ── File watcher ─────────────────────────────────────────────────────────────

class VaultHandler(FileSystemEventHandler):
    def __init__(self):
        self._conn = None

    @property
    def conn(self):
        if self._conn is None:
            self._conn = open_db()
        return self._conn

    def on_modified(self, event):
        if not event.is_directory:
            upsert_file(self.conn, Path(event.src_path))

    def on_created(self, event):
        if not event.is_directory:
            upsert_file(self.conn, Path(event.src_path))

    def on_deleted(self, event):
        if not event.is_directory:
            delete_file(self.conn, Path(event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            delete_file(self.conn, Path(event.src_path))
            upsert_file(self.conn, Path(event.dest_path))

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reindex", action="store_true", help="Full reindex before watching")
    parser.add_argument("--watch", action="store_true", help="Stay in watch mode after reindex")
    args = parser.parse_args()

    watch_mode = args.watch or not args.reindex  # default is watch-only

    (TAM_HOME / "logs").mkdir(exist_ok=True)
    conn = open_db()
    init_db(conn)

    if args.reindex:
        reindex(conn)

    if watch_mode:
        handler = VaultHandler()
        observer = Observer()
        for vault_path in WATCH_PATHS:
            if vault_path.exists():
                observer.schedule(handler, str(vault_path), recursive=True)
                log.info(f"Watching: {vault_path}")
        observer.start()
        log.info("Indexer running. Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()
        conn.close()


if __name__ == "__main__":
    main()
