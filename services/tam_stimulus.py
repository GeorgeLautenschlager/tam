"""
tam-stimulus.py — Stimulus observation and processing for Tam's LangGraph supervisor.

Three-layer architecture:
  1. SenseAdapters   — source-specific pollers (Discord, filesystem, schedule, task queue).
                       Each adapter yields Stimulus objects without touching the DB.
  2. StimulusObserver — pure DB writer. Thread-safe. Zero intelligence. Receives Stimulus
                        objects from adapters and persists them to SQLite.
  3. StimulusProcessor — reads unprocessed stimuli, batches them by (source, channel), and
                         returns a StimulusContext for the DECIDE node in the LangGraph graph.

This separation keeps I/O, persistence, and reasoning cleanly decoupled.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional
from typing import runtime_checkable, Protocol

logger = logging.getLogger("tam-stimulus")


# ---------------------------------------------------------------------------
# 1. Stimulus dataclass
# ---------------------------------------------------------------------------

@dataclass
class Stimulus:
    source: str                         # 'discord', 'filesystem', 'schedule', 'taskqueue'
    content: str                        # human-readable description
    source_id: Optional[str] = None     # dedup key (discord msg ID, file path, etc.)
    channel: Optional[str] = None       # batching/grouping key
    metadata: dict = field(default_factory=dict)
    priority: int = 0                   # 1=high (George), 0=normal, -1=low
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def metadata_json(self) -> str:
        return json.dumps(self.metadata)


# ---------------------------------------------------------------------------
# 2. SenseAdapter Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class SenseAdapter(Protocol):
    name: str

    def poll(self) -> Iterator[Stimulus]: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


# ---------------------------------------------------------------------------
# 3. StimulusObserver — pure DB writer, thread-safe
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stimuli (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    source_id    TEXT,
    channel      TEXT,
    content      TEXT NOT NULL,
    metadata     TEXT,
    priority     INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL,
    processed_at TEXT,
    batch_id     TEXT
);

CREATE INDEX IF NOT EXISTS idx_stimuli_unprocessed
    ON stimuli(processed_at)
    WHERE processed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_stimuli_source_channel
    ON stimuli(source, channel);

CREATE UNIQUE INDEX IF NOT EXISTS idx_stimuli_dedup
    ON stimuli(source, source_id)
    WHERE source_id IS NOT NULL;
"""

_INSERT_SQL = """
INSERT OR IGNORE INTO stimuli
    (source, source_id, channel, content, metadata, priority, created_at)
VALUES
    (:source, :source_id, :channel, :content, :metadata, :priority, :created_at)
"""


class StimulusObserver:
    """Thread-safe SQLite writer for incoming Stimulus objects.

    Lazily opens a single connection per instance with WAL journal mode
    and a 5-second busy timeout so concurrent readers don't block writes.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        assert self._conn is not None
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()
        logger.debug("stimuli schema initialised at %s", self._db_path)

    @staticmethod
    def _row(stimulus: Stimulus) -> dict:
        return {
            "source": stimulus.source,
            "source_id": stimulus.source_id,
            "channel": stimulus.channel,
            "content": stimulus.content,
            "metadata": stimulus.metadata_json(),
            "priority": stimulus.priority,
            "created_at": stimulus.created_at,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, stimulus: Stimulus) -> None:
        """Persist a single stimulus. Silently ignores duplicates."""
        with self._lock:
            conn = self._connection()
            conn.execute(_INSERT_SQL, self._row(stimulus))
            conn.commit()
        logger.debug("recorded stimulus source=%s source_id=%s", stimulus.source, stimulus.source_id)

    def record_many(self, stimuli: list[Stimulus]) -> None:
        """Batch-persist a list of stimuli. Silently ignores duplicates."""
        if not stimuli:
            return
        rows = [self._row(s) for s in stimuli]
        with self._lock:
            conn = self._connection()
            conn.executemany(_INSERT_SQL, rows)
            conn.commit()
        logger.debug("recorded %d stimuli", len(stimuli))

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                logger.debug("StimulusObserver connection closed")


# ---------------------------------------------------------------------------
# 4. StimulusProcessor — SENSE replacement for LangGraph
# ---------------------------------------------------------------------------

@dataclass
class StimulusBatch:
    source: str
    channel: Optional[str]
    items: list[str]        # the content strings
    priority: int           # max priority in batch
    count: int
    metadata: list[dict]


@dataclass
class StimulusContext:
    batches: list[StimulusBatch]
    batch_id: str           # UUID for audit trail
    total_count: int        # total stimuli processed this cycle
    has_high_priority: bool # any priority >= 1?

    def to_dict(self) -> dict:
        """Serialize to a plain dict suitable for storage in LangGraph TypedDict state."""
        return {
            "batch_id": self.batch_id,
            "total_count": self.total_count,
            "has_high_priority": self.has_high_priority,
            "batches": [
                {
                    "source": b.source,
                    "channel": b.channel,
                    "items": b.items,
                    "priority": b.priority,
                    "count": b.count,
                    "metadata": b.metadata,
                }
                for b in self.batches
            ],
        }


_SELECT_UNPROCESSED = """
SELECT id, source, source_id, channel, content, metadata, priority, created_at
FROM stimuli
WHERE processed_at IS NULL
ORDER BY priority DESC, created_at ASC
"""

_STALE_THRESHOLD_HOURS = 4


class StimulusProcessor:
    """Reads unprocessed stimuli, batches them, and returns a StimulusContext.

    This replaces the SENSE node in the LangGraph supervisor graph.
    Stale stimuli (older than 4 hours) are marked processed with batch_id='stale'
    and excluded from the returned context.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def process(self) -> StimulusContext:
        """Fetch, filter, batch, and mark unprocessed stimuli.

        Returns an empty StimulusContext when there is nothing to process.
        """
        conn = self._connection()
        rows = conn.execute(_SELECT_UNPROCESSED).fetchall()

        if not rows:
            logger.debug("process(): no unprocessed stimuli")
            return StimulusContext(batches=[], batch_id="", total_count=0, has_high_priority=False)

        now = datetime.now(timezone.utc)
        stale_cutoff = now - timedelta(hours=_STALE_THRESHOLD_HOURS)

        stale_ids: list[int] = []
        active_rows: list[sqlite3.Row] = []

        for row in rows:
            try:
                created = datetime.fromisoformat(row["created_at"])
                # Ensure timezone-aware comparison
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except ValueError:
                logger.warning("unparseable created_at for id=%s, treating as active", row["id"])
                active_rows.append(row)
                continue

            if created < stale_cutoff:
                stale_ids.append(row["id"])
            else:
                active_rows.append(row)

        now_iso = now.isoformat()

        # Mark stale rows
        if stale_ids:
            placeholders = ",".join("?" * len(stale_ids))
            conn.execute(
                f"UPDATE stimuli SET processed_at = ?, batch_id = 'stale' WHERE id IN ({placeholders})",
                [now_iso] + stale_ids,
            )
            logger.info("marked %d stimuli as stale", len(stale_ids))

        if not active_rows:
            conn.commit()
            return StimulusContext(batches=[], batch_id="", total_count=0, has_high_priority=False)

        batch_id = str(uuid.uuid4())

        # Group by (source, channel)
        groups: dict[tuple[str, Optional[str]], list[sqlite3.Row]] = {}
        for row in active_rows:
            key = (row["source"], row["channel"])
            groups.setdefault(key, []).append(row)

        batches: list[StimulusBatch] = []
        for (source, channel), group_rows in groups.items():
            items = [r["content"] for r in group_rows]
            max_priority = max(r["priority"] for r in group_rows)
            metadata_list: list[dict] = []
            for r in group_rows:
                raw = r["metadata"]
                try:
                    metadata_list.append(json.loads(raw) if raw else {})
                except (json.JSONDecodeError, TypeError):
                    metadata_list.append({})
            batches.append(
                StimulusBatch(
                    source=source,
                    channel=channel,
                    items=items,
                    priority=max_priority,
                    count=len(items),
                    metadata=metadata_list,
                )
            )

        # Sort batches: highest priority first
        batches.sort(key=lambda b: b.priority, reverse=True)

        # Mark active rows as processed
        active_ids = [row["id"] for row in active_rows]
        placeholders = ",".join("?" * len(active_ids))
        conn.execute(
            f"UPDATE stimuli SET processed_at = ?, batch_id = ? WHERE id IN ({placeholders})",
            [now_iso, batch_id] + active_ids,
        )
        conn.commit()

        has_high_priority = any(b.priority >= 1 for b in batches)
        total = len(active_rows)

        logger.info(
            "process(): batch_id=%s total=%d batches=%d high_priority=%s",
            batch_id,
            total,
            len(batches),
            has_high_priority,
        )

        return StimulusContext(
            batches=batches,
            batch_id=batch_id,
            total_count=total,
            has_high_priority=has_high_priority,
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            logger.debug("StimulusProcessor connection closed")
