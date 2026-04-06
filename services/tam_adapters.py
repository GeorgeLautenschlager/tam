"""
tam-adapters.py — Sense adapters for Tam's stimulus pipeline.

Each adapter knows how to listen to a specific stimulus source and produce
uniform Stimulus objects for the StimulusObserver. Three adapters are provided:

  - TaskQueueSense   poll-based; reads vault task queue and priorities files
  - ScheduleSense    poll-based; monitors system state transitions
  - FileSystemSense  push-based; uses watchdog to watch directories for file changes
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent
from watchdog.observers import Observer

from tam_stimulus import Stimulus, StimulusObserver

logger = logging.getLogger("tam-adapters")

_DEFAULT_VAULT = Path("~/vaults/tam").expanduser()
_DEFAULT_TAM_HOME = Path("~/tam").expanduser()


# ---------------------------------------------------------------------------
# TaskQueueSense
# ---------------------------------------------------------------------------

class TaskQueueSense:
    """Poll-based adapter that reads vault task-queue and priorities markdown files.

    Emits Stimulus objects for uncompleted items. Tracks seen IDs so the same
    item is only emitted once per lifecycle; resets tracking when items disappear
    (indicating completion).
    """

    name: str = "taskqueue"

    def __init__(self, vault_root: Path = _DEFAULT_VAULT) -> None:
        self._vault_root = vault_root
        self._task_queue_file = vault_root / "State" / "Task Queue.md"
        self._priorities_file = vault_root / "State" / "Priorities.md"
        self._seen_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def poll(self) -> Iterator[Stimulus]:
        """Yield new stimuli for queued tasks and active/queued priorities."""
        current_ids: set[str] = set()
        stimuli: list[Stimulus] = []

        for stimulus in self._read_task_queue():
            current_ids.add(stimulus.source_id)  # type: ignore[arg-type]
            stimuli.append(stimulus)

        for stimulus in self._read_priorities():
            current_ids.add(stimulus.source_id)  # type: ignore[arg-type]
            stimuli.append(stimulus)

        # Reset seen IDs for items that have disappeared (completed)
        disappeared = self._seen_ids - current_ids
        if disappeared:
            logger.debug("TaskQueueSense: %d items disappeared (completed), resetting", len(disappeared))
            self._seen_ids -= disappeared

        for stimulus in stimuli:
            sid = stimulus.source_id
            if sid not in self._seen_ids:
                self._seen_ids.add(sid)  # type: ignore[arg-type]
                yield stimulus

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_task_queue(self) -> list[Stimulus]:
        if not self._task_queue_file.exists():
            logger.debug("TaskQueueSense: task queue file not found: %s", self._task_queue_file)
            return []

        content = self._task_queue_file.read_text(encoding="utf-8")
        queued_section = re.search(r"## Queued\s*\n(.*?)(?=## |\Z)", content, re.DOTALL)
        if not queued_section:
            return []

        items = re.findall(r"^- \[ \] (.+)$", queued_section.group(1), re.MULTILINE)
        stimuli = []
        for text in items:
            text = text.strip()
            source_id = f"taskqueue::{hashlib.md5(text.encode()).hexdigest()[:12]}"
            stimuli.append(Stimulus(
                source="taskqueue",
                content=text,
                source_id=source_id,
                channel="task_queue",
                metadata={"file": str(self._task_queue_file), "section": "Queued"},
                priority=1,
            ))
        return stimuli

    def _read_priorities(self) -> list[Stimulus]:
        if not self._priorities_file.exists():
            logger.debug("TaskQueueSense: priorities file not found: %s", self._priorities_file)
            return []

        content = self._priorities_file.read_text(encoding="utf-8")
        stimuli = []

        for section in ("Active", "Queued"):
            section_match = re.search(
                rf"## {section}\s*\n(.*?)(?=## |\Z)", content, re.DOTALL
            )
            if not section_match:
                continue
            items = re.findall(r"^- \[ \] (.+)$", section_match.group(1), re.MULTILINE)
            for text in items:
                text = text.strip()
                source_id = f"priority::{hashlib.md5(text.encode()).hexdigest()[:12]}"
                stimuli.append(Stimulus(
                    source="taskqueue",
                    content=text,
                    source_id=source_id,
                    channel="priorities",
                    metadata={"file": str(self._priorities_file), "section": section},
                    priority=0,
                ))
        return stimuli


# ---------------------------------------------------------------------------
# ScheduleSense
# ---------------------------------------------------------------------------

class ScheduleSense:
    """Poll-based adapter that monitors system state and emits stimuli on transitions.

    Watches:
      - .tam-pause file existence
      - Budget thresholds via tam-budget.py
    """

    name: str = "schedule"

    def __init__(self, tam_home: Path = _DEFAULT_TAM_HOME) -> None:
        self._tam_home = tam_home
        self._pause_file = tam_home / ".tam-pause"
        self._schedule_file = tam_home / "schedule.json"
        self._budget_script = tam_home / "tam-budget.py"
        self._prev_state: dict = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def poll(self) -> Iterator[Stimulus]:
        """Yield stimuli for system state transitions since last poll."""
        now = datetime.now()
        current_state = self._sample_state(now)

        for event_type, stimulus in self._compute_transitions(current_state, now):
            yield stimulus

        self._prev_state = current_state

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sample_state(self, now: datetime) -> dict:
        paused = self._pause_file.exists()
        budget = self._check_budget()
        return {
            "paused": paused,
            "budget_daily_over": budget.get("daily_over_50", False),
            "budget_weekly_over": budget.get("weekly_over_50", False),
        }

    def _compute_transitions(
        self, current: dict, now: datetime
    ) -> list[tuple[str, Stimulus]]:
        results = []

        def _transition(key: str, event_on: str, event_off: str) -> None:
            prev_val = self._prev_state.get(key)
            curr_val = current[key]
            if prev_val is None:
                # First poll — establish baseline without emitting
                return
            if curr_val == prev_val:
                return
            event_type = event_on if curr_val else event_off
            source_id = f"schedule::{event_type}::{now.strftime('%Y%m%d%H')}"
            stimulus = Stimulus(
                source="schedule",
                content=event_type.replace("_", " ").capitalize(),
                source_id=source_id,
                channel="system",
                metadata={"event": event_type, "key": key},
                priority=0,
            )
            results.append((event_type, stimulus))

        _transition("paused", "system_paused", "system_unpaused")
        _transition("budget_daily_over", "budget_daily_threshold_crossed", "budget_daily_threshold_cleared")
        _transition("budget_weekly_over", "budget_weekly_threshold_crossed", "budget_weekly_threshold_cleared")

        return results

    def _check_budget(self) -> dict:
        """Run tam-budget.py and return parsed thresholds. Fails open (returns {})."""
        if not self._budget_script.exists():
            return {}
        try:
            result = subprocess.run(
                ["python3", str(self._budget_script), "--source", "cron"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return {}
            data = json.loads(result.stdout)
            daily_pct = data.get("daily_pct", 0)
            weekly_pct = data.get("weekly_pct", 0)
            return {
                "daily_over_50": daily_pct > 50,
                "weekly_over_50": weekly_pct > 50,
            }
        except Exception as exc:
            logger.debug("ScheduleSense: budget check failed (fail-open): %s", exc)
            return {}


# ---------------------------------------------------------------------------
# FileSystemSense
# ---------------------------------------------------------------------------

class _FileChangeHandler(FileSystemEventHandler):
    """Watchdog event handler that records .md file changes as Stimulus objects."""

    def __init__(self, observer: StimulusObserver) -> None:
        super().__init__()
        self._observer = observer

    def on_modified(self, event) -> None:
        if not isinstance(event, FileModifiedEvent):
            return
        self._handle(event.src_path, "modified")

    def on_created(self, event) -> None:
        if not isinstance(event, FileCreatedEvent):
            return
        self._handle(event.src_path, "created")

    def _handle(self, src_path: str, event_type: str) -> None:
        path = Path(src_path)
        if path.suffix.lower() != ".md":
            return

        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0

        source_id = f"{path}::{mtime:.0f}"
        content = f"File {event_type}: {path.name}"
        channel = path.parent.name

        stimulus = Stimulus(
            source="filesystem",
            content=content,
            source_id=source_id,
            channel=channel,
            metadata={"path": str(path), "event": event_type},
            priority=0,
        )
        logger.debug("FileSystemSense: %s", content)
        self._observer.record(stimulus)


class FileSystemSense:
    """Push-based adapter that uses watchdog to watch directories for file changes.

    Calls observer.record() directly from the watchdog event handler thread.
    poll() returns an empty iterator — stimuli are pushed asynchronously.
    """

    name: str = "filesystem"

    def __init__(
        self,
        observer: StimulusObserver,
        watch_paths: list[Path] | None = None,
    ) -> None:
        self._stimulus_observer = observer
        if watch_paths is None:
            watch_paths = [Path("~/vaults/tam/State").expanduser()]
        self._watch_paths = watch_paths
        self._watchdog_observer: Observer | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the watchdog observer and schedule handlers for all watch paths."""
        handler = _FileChangeHandler(self._stimulus_observer)
        self._watchdog_observer = Observer()
        for watch_path in self._watch_paths:
            if not watch_path.exists():
                logger.warning("FileSystemSense: watch path does not exist: %s", watch_path)
                continue
            self._watchdog_observer.schedule(handler, str(watch_path), recursive=False)
            logger.info("FileSystemSense: watching %s", watch_path)
        self._watchdog_observer.start()
        logger.info("FileSystemSense: watchdog observer started")

    def stop(self) -> None:
        """Stop and join the watchdog observer."""
        if self._watchdog_observer is not None:
            self._watchdog_observer.stop()
            self._watchdog_observer.join()
            self._watchdog_observer = None
            logger.info("FileSystemSense: watchdog observer stopped")

    def poll(self) -> Iterator[Stimulus]:
        """No-op for push-based adapter — yields nothing."""
        return
        yield  # make this a generator
