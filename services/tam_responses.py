"""
tam_responses.py — Response routing for Tam's stimulus pipeline.

Adds a return path so the supervisor can route responses back to the
originating source/channel (web UI, Discord, etc.).

The responses table lives in the same stimuli.db to keep things simple.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tam-responses")


_RESPONSE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS responses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    stimulus_id  INTEGER,
    batch_id     TEXT,
    source       TEXT NOT NULL,
    channel      TEXT NOT NULL,
    content      TEXT,
    metadata     TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_responses_pending
    ON responses(channel, status)
    WHERE status != 'complete';

CREATE INDEX IF NOT EXISTS idx_responses_batch
    ON responses(batch_id)
    WHERE batch_id IS NOT NULL;
"""


class ResponseWriter:
    """Thread-safe reader/writer for the responses table.

    Follows the same patterns as StimulusObserver: WAL mode, busy timeout,
    lazy connection, lock-protected writes.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_RESPONSE_SCHEMA_SQL)
            self._conn.commit()
            logger.debug("responses schema initialised at %s", self._db_path)
        return self._conn

    # ------------------------------------------------------------------
    # Write operations (used by tam-web and supervisor)
    # ------------------------------------------------------------------

    def create_pending(self, stimulus_id: int, source: str, channel: str) -> int:
        """Create a pending response row. Returns the response ID."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._connection()
            cur = conn.execute(
                "INSERT INTO responses (stimulus_id, source, channel, status, created_at)"
                " VALUES (?, ?, ?, 'pending', ?)",
                (stimulus_id, source, channel, now),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def mark_processing(self, batch_id: str, source: str) -> None:
        """Mark all pending responses for a source as processing, tagging with batch_id."""
        with self._lock:
            conn = self._connection()
            conn.execute(
                "UPDATE responses SET status = 'processing', batch_id = ?"
                " WHERE source = ? AND status = 'pending'",
                (batch_id, source),
            )
            conn.commit()

    def complete(self, batch_id: str, content: str, metadata: Optional[dict] = None) -> int:
        """Complete all responses in a batch. Returns number of rows updated."""
        now = datetime.now(timezone.utc).isoformat()
        meta_json = json.dumps(metadata) if metadata else None
        with self._lock:
            conn = self._connection()
            cur = conn.execute(
                "UPDATE responses SET content = ?, metadata = ?, status = 'complete',"
                " completed_at = ? WHERE batch_id = ? AND status = 'processing'",
                (content, meta_json, now, batch_id),
            )
            conn.commit()
            return cur.rowcount

    def complete_by_channel(self, channel: str, content: str, metadata: Optional[dict] = None) -> int:
        """Complete all pending/processing responses for a channel."""
        now = datetime.now(timezone.utc).isoformat()
        meta_json = json.dumps(metadata) if metadata else None
        with self._lock:
            conn = self._connection()
            cur = conn.execute(
                "UPDATE responses SET content = ?, metadata = ?, status = 'complete',"
                " completed_at = ? WHERE channel = ? AND status IN ('pending', 'processing')",
                (content, meta_json, now, channel),
            )
            conn.commit()
            return cur.rowcount

    def error(self, batch_id: str, error_msg: str) -> int:
        """Mark batch responses as errored."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._connection()
            cur = conn.execute(
                "UPDATE responses SET content = ?, status = 'error',"
                " completed_at = ? WHERE batch_id = ? AND status IN ('pending', 'processing')",
                (error_msg, now, batch_id),
            )
            conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # Read operations (used by tam-web SSE endpoint)
    # ------------------------------------------------------------------

    def get_pending(self, channel: str) -> list[dict]:
        """Return pending/processing responses for a channel."""
        conn = self._connection()
        rows = conn.execute(
            "SELECT id, stimulus_id, status, created_at FROM responses"
            " WHERE channel = ? AND status IN ('pending', 'processing')"
            " ORDER BY created_at ASC",
            (channel,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_complete_since(self, channel: str, after_id: int = 0) -> list[dict]:
        """Return completed responses after a given ID (for SSE polling)."""
        conn = self._connection()
        rows = conn.execute(
            "SELECT id, stimulus_id, batch_id, content, metadata, completed_at"
            " FROM responses"
            " WHERE channel = ? AND status = 'complete' AND id > ?"
            " ORDER BY id ASC",
            (channel, after_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_history(self, channel: str, limit: int = 50) -> list[dict]:
        """Return recent stimulus+response pairs for a channel.

        Joins against the stimuli table to get the original message content.
        """
        conn = self._connection()
        rows = conn.execute(
            "SELECT r.id, r.stimulus_id, r.content as response,"
            "       r.status, r.created_at, r.completed_at,"
            "       s.content as message"
            " FROM responses r"
            " LEFT JOIN stimuli s ON r.stimulus_id = s.id"
            " WHERE r.channel = ?"
            " ORDER BY r.id DESC LIMIT ?",
            (channel, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
