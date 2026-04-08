#!/usr/bin/env python3
"""
tam-supervisor.py — Tam's autonomous agency loop

A LangGraph state machine that runs continuously as a systemd service.
Unlike the cron model (reactive, George-driven), this is proactive:
Tam has a priorities list it manages itself and can defer requests
when it's busy with something that matters more.

Architecture:
    SENSE (free, ~60s) → DECIDE (rules-based) → ACT (spawns tam.sh)
                  ↑                                     ↓
                  └──────────── REFLECT ←───────────────┘

Key files:
    ~/vaults/tam/State/Priorities.md   — self-managed agenda
    ~/vaults/tam/State/Task Queue.md   — George's requests (higher priority)
    ~/tam/STATE.md                     — hot state
    ~/tam/.tam.lock                    — prevents double-running with cron

Usage:
    python3 tam-supervisor.py              # run (exits after single act cycle for now)
    python3 tam-supervisor.py --loop       # run continuous loop
    python3 tam-supervisor.py --dry-run    # sense + decide, don't act
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional
from typing_extensions import TypedDict

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tam_stimulus import StimulusProcessor
from tam_responses import ResponseWriter
from tam_llm_runner import run_local

# ── Config ────────────────────────────────────────────────────────────────────

TAM_HOME = Path(os.environ.get("TAM_HOME", Path.home() / "tam"))
VAULT_ROOT = Path.home() / "vaults" / "tam"

TASK_QUEUE_FILE = VAULT_ROOT / "State" / "Task Queue.md"
PRIORITIES_FILE = VAULT_ROOT / "State" / "Priorities.md"
STATE_FILE = TAM_HOME / "STATE.md"
LOCK_FILE = TAM_HOME / ".tam.lock"
PAUSE_FILE = TAM_HOME / ".tam-pause"
SCHEDULE_FILE = TAM_HOME / "data" / "schedule.json"
STIMULI_DB = TAM_HOME / "stimuli.db"
CHECKPOINT_DB = TAM_HOME / "data" / "supervisor-state.db"
SUPERVISOR_THREAD_ID = "supervisor"   # LangGraph thread ID for checkpoint scoping

# Shared instances — initialised in main()
stimulus_processor: Optional[StimulusProcessor] = None
response_writer: Optional[ResponseWriter] = None

# ── Checkpoint helpers ────────────────────────────────────────────────────────

def checkpoint_config() -> dict:
    return {"configurable": {"thread_id": SUPERVISOR_THREAD_ID, "checkpoint_ns": ""}}


def write_checkpoint(checkpointer, state: "SupervisorState") -> None:
    """Persist supervisor state to SQLite checkpoint."""
    try:
        cfg = checkpoint_config()
        cp = {
            "v": 1,
            "id": str(uuid.uuid4()),
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "channel_values": dict(state),
            "channel_versions": {},
            "versions_seen": {},
            "updated_channels": [],
        }
        checkpointer.put(cfg, cp, {}, {})
    except Exception as e:
        log.warning(f"Checkpoint write failed: {e}")


def read_checkpoint(checkpointer) -> dict:
    """Read last persisted state. Returns {} if none."""
    try:
        stored = checkpointer.get(checkpoint_config())
        return stored.get("channel_values", {}) if stored else {}
    except Exception as e:
        log.warning(f"Checkpoint read failed: {e}")
        return {}

SENSE_INTERVAL_SECONDS = 60       # How often SENSE runs
ACT_TIMEOUT_SECONDS = 600         # Max time for an ACT invocation
ACT_MIN_GAP_SECONDS = 120         # Minimum gap between consecutive ACT calls

# ── Cognitive Modes ──────────────────────────────────────────────────────────
#
# Tam's identity persists across models: SOUL.md + memories + behavioral patterns.
# Different models are different *modes of cognition*, not different identities.
#
#   Deliberate (Opus)  — deep reasoning, nuanced judgment, long planning horizons
#   Flow (Sonnet)      — practical execution, good enough for most work
#   Reflex (Haiku)     — lightweight, reactive, high-throughput
#
# Budget pressure causes graceful downshift: Opus→Sonnet→Haiku.

COGNITIVE_MODES = {
    "deliberate": "opus",    # George's tasks, architectural work, identity/soul
    "flow":      "sonnet",   # Self-directed priorities, standard dev
    "reflex":    "haiku",    # Routine checks, ingestion, simple updates
}

# Budget thresholds for downshifting (% of daily budget used)
DOWNSHIFT_THRESHOLD = 30   # Above this, step each mode down one tier


def select_cognitive_mode(task_type: str, budget_pct: float, budget_tier: str = "normal") -> str:
    """Choose the right model based on task type and budget pressure.

    Returns a model shorthand: 'opus', 'sonnet', or 'haiku'.

    budget_tier overrides the legacy budget_pct threshold when provided:
        minimal  → always haiku
        conserve → downshift one tier (opus→sonnet, sonnet→haiku, haiku→haiku)
        normal   → no change (legacy DOWNSHIFT_THRESHOLD still applies as fallback)
    """
    base_mode = {
        "george_task":  "deliberate",
        "priority":     "flow",
        "routine":      "reflex",
    }.get(task_type, "flow")

    model = COGNITIVE_MODES[base_mode]
    downshift_map = {"opus": "sonnet", "sonnet": "haiku", "haiku": "haiku"}

    if budget_tier == "minimal":
        model = "haiku"
        log.info(f"COGNITION: budget minimal, forcing haiku ({base_mode} overridden)")
    elif budget_tier == "conserve":
        downshifted = downshift_map[model]
        log.info(f"COGNITION: budget conserve, downshift {base_mode} ({model}) → {downshifted}")
        model = downshifted
    elif budget_pct > DOWNSHIFT_THRESHOLD:
        # Legacy fallback: downshift on budget pressure when tier is "normal"
        model = downshift_map[model]
        log.info(f"COGNITION: budget pressure ({budget_pct:.0f}%), downshift {base_mode} → {model}")
    else:
        log.info(f"COGNITION: {base_mode} → {model}")

    return model

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [supervisor] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tam-supervisor")


# ── State ─────────────────────────────────────────────────────────────────────

class SupervisorState(TypedDict):
    # What SENSE found
    has_queued_tasks: bool
    queued_tasks: list[str]
    has_active_priority: bool
    active_priority: str
    queued_priorities: list[str]
    paused: bool
    hour: int
    last_act_timestamp: float   # unix time of last ACT call

    # What DECIDE produced
    decision: Literal["act", "wait", "pause"]
    act_reason: str
    act_prompt: str
    act_model: str              # sonnet | opus | haiku

    # What ACT produced
    act_result: str
    act_cost: float
    act_session_id: str

    # Stimulus pipeline
    stimulus_batch_id: str          # batch_id from StimulusProcessor
    stimulus_response_channel: str  # channel to route response back to (e.g. "web")
    has_interactive_stimulus: bool   # web/discord message waiting for a reply?

    # Loop control
    iteration: int


# ── Helpers ───────────────────────────────────────────────────────────────────

def read_task_queue() -> list[str]:
    """Return list of queued task descriptions from Task Queue.md."""
    if not TASK_QUEUE_FILE.exists():
        return []
    content = TASK_QUEUE_FILE.read_text()
    # Look for items under "## Queued" section
    queued_section = re.search(r"## Queued\s*\n(.*?)(?=## |\Z)", content, re.DOTALL)
    if not queued_section:
        return []
    lines = queued_section.group(1).strip().splitlines()
    tasks = [l.strip() for l in lines if l.strip().startswith("- [ ]")]
    return [t.lstrip("- [ ]").strip() for t in tasks]


def read_priorities() -> tuple[str, list[str]]:
    """Return (active_priority, queued_priorities) from Priorities.md."""
    if not PRIORITIES_FILE.exists():
        return "", []

    content = PRIORITIES_FILE.read_text()

    # Active section
    active_match = re.search(r"## Active\s*\n(.*?)(?=## |\Z)", content, re.DOTALL)
    active = ""
    if active_match:
        lines = active_match.group(1).strip().splitlines()
        items = [l.strip() for l in lines if l.strip().startswith("- [ ]")]
        if items:
            active = items[0].lstrip("- [ ]").strip()

    # Queued section
    queued_match = re.search(r"## Queued\s*\n(.*?)(?=## |\Z)", content, re.DOTALL)
    queued = []
    if queued_match:
        lines = queued_match.group(1).strip().splitlines()
        items = [l.strip() for l in lines if l.strip().startswith("- [ ]")]
        queued = [i.lstrip("- [ ]").strip() for i in items]

    return active, queued


def is_paused() -> bool:
    return PAUSE_FILE.exists()


def is_quiet_hours(hour: int) -> bool:
    """Check if we're in quiet hours per schedule.json."""
    if not SCHEDULE_FILE.exists():
        return hour < 7 or hour >= 22
    try:
        sched = json.loads(SCHEDULE_FILE.read_text())
        quiet_start = sched.get("quiet_hours", {}).get("start", 22)
        quiet_end = sched.get("quiet_hours", {}).get("end", 7)
        if quiet_start > quiet_end:
            return hour >= quiet_start or hour < quiet_end
        return quiet_start <= hour < quiet_end
    except Exception:
        return hour < 7 or hour >= 22


def check_budget() -> dict:
    """Run tam-budget.py and return the parsed JSON."""
    try:
        result = subprocess.run(
            ["python3", str(TAM_HOME / "scripts" / "tam-budget.py"), "--source", "cron"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        return {"allowed": False, "reason": f"budget exit={result.returncode}"}
    except Exception as e:
        log.warning(f"Budget check failed: {e}")
        return {"allowed": True, "reason": "budget-check-error-failsafe"}


def classify_budget(daily_pct: float, weekly_pct: float) -> str:
    """Return a budget tier string based on daily and weekly usage percentages.

    Tiers (worst of daily/weekly):
        hard_stop  — >85%: interactive gets canned response, autonomous suspended
        suspended  — >70%: autonomous suspended, interactive haiku only
        minimal    — >50%: autonomous haiku only, interactive downshifted
        conserve   — >30%: autonomous downshifted one tier
        normal     — <=30%: no restrictions
    """
    worst = max(daily_pct, weekly_pct)
    if worst > 85:
        return "hard_stop"
    if worst > 70:
        return "suspended"
    if worst > 50:
        return "minimal"
    if worst > 30:
        return "conserve"
    return "normal"


def run_claude(prompt: str, model: str = "sonnet", timeout: int = ACT_TIMEOUT_SECONDS) -> dict:
    """Invoke Claude Code headless via claude CLI. Returns {result, cost, session_id}."""
    # Load env
    env_file = TAM_HOME / ".env"
    env = os.environ.copy()
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()

    # Model shorthand resolution
    model_map = {
        "opus": "claude-opus-4-6",
        "sonnet": "claude-sonnet-4-6",
        "haiku": "claude-haiku-4-5-20251001",
    }
    resolved_model = model_map.get(model, model)

    soul_file = TAM_HOME / "docs" / "SOUL.md"
    system_prompt = soul_file.read_text() if soul_file.exists() else ""

    cmd = [
        "claude", "-p", prompt,
        "--model", resolved_model,
        "--allowedTools", "Read,Write,Edit,Glob,Grep,Bash",
        "--dangerously-skip-permissions",
        "--max-turns", "50",
        "--output-format", "json",
    ]
    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])

    log.info(f"ACT: invoking Claude ({model}) — {prompt[:80]}...")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, cwd=str(TAM_HOME), env=env
        )
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        return {
            "result": data.get("result", ""),
            "cost": data.get("total_cost_usd", 0.0),
            "session_id": data.get("session_id", ""),
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"result": "[timed out]", "cost": 0.0, "session_id": "", "exit_code": -1}
    except Exception as e:
        return {"result": f"[error: {e}]", "cost": 0.0, "session_id": "", "exit_code": -1}


# ── Graph nodes ───────────────────────────────────────────────────────────────

def sense(state: SupervisorState) -> SupervisorState:
    """SENSE: Free, frequent. Read environment and stimulus pipeline."""
    log.debug("SENSE")

    now = datetime.now()  # local time — quiet hours are configured in local tz

    queued_tasks = read_task_queue()
    active_priority, queued_priorities = read_priorities()

    # Check stimulus pipeline for interactive messages (web, discord)
    has_interactive = False
    batch_id = ""
    response_channel = ""

    if stimulus_processor is not None:
        ctx = stimulus_processor.process()
        if ctx.total_count > 0:
            batch_id = ctx.batch_id
            log.info(f"SENSE: {ctx.total_count} stimuli in batch {batch_id[:8]}")
            for batch in ctx.batches:
                if batch.source in ("web", "discord"):
                    has_interactive = True
                    response_channel = batch.channel or batch.source
                    # Prepend interactive messages to the task list so DECIDE sees them
                    for item in batch.items:
                        queued_tasks.insert(0, f"[{batch.source}] {item}")
                    break  # handle one interactive batch per cycle

    return {
        **state,
        "has_queued_tasks": len(queued_tasks) > 0,
        "queued_tasks": queued_tasks,
        "has_active_priority": bool(active_priority),
        "active_priority": active_priority,
        "queued_priorities": queued_priorities,
        "paused": is_paused(),
        "hour": now.hour,
        "iteration": state.get("iteration", 0) + 1,
        "has_interactive_stimulus": has_interactive,
        "stimulus_batch_id": batch_id,
        "stimulus_response_channel": response_channel,
    }


def decide(state: SupervisorState) -> SupervisorState:
    """DECIDE: Rules-based. Should we act, wait, or pause?"""
    log.debug("DECIDE")

    # Interactive stimuli (web/discord) get top priority — George is waiting.
    # They bypass quiet hours and min gap. Budget uses DAILY pct only (not weekly)
    # so a heavy week doesn't silence George mid-day.
    if state.get("has_interactive_stimulus"):
        messages = [t for t in state["queued_tasks"] if t.startswith("[web]") or t.startswith("[discord]")]
        combined = "\n".join(m.split("] ", 1)[1] for m in messages) if messages else ""
        source = "web" if any(t.startswith("[web]") for t in messages) else "discord"

        budget = check_budget()
        daily_pct = budget.get("daily_pct", 0)
        weekly_pct = budget.get("weekly_pct", 0)
        # For interactive: tier is based on daily only (weekly tension resolved here)
        interactive_tier = classify_budget(daily_pct, daily_pct)

        if interactive_tier == "hard_stop":
            log.info(f"DECIDE → wait (interactive {source}: hard_stop, daily={daily_pct:.0f}%)")
            channel = state.get("stimulus_response_channel", "")
            if response_writer is not None and channel:
                response_writer.complete_by_channel(
                    channel,
                    "Budget's tapped out for today. I can see your message — "
                    "I'll pick it up when the daily budget resets.",
                    {"model": "none", "cost": 0},
                )
            return {
                **state,
                "decision": "wait",
                "act_reason": f"interactive {source}: hard_stop (daily={daily_pct:.0f}%)",
            }

        log.info(f"DECIDE → act (interactive {source}: {combined[:60]}, tier={interactive_tier})")
        prompt = (
            "You are Tam. George is messaging you directly via the local web interface. "
            "Read BOOT.md and follow the wake-up sequence, but keep it lightweight — "
            "George is waiting for a reply.\n\n"
            f"George's message:\n{combined}\n\n"
            "Respond conversationally. Keep it concise. "
            "Read STATE.md or MEMORY.md if you need context, but don't do it for "
            "simple questions or casual chat — use judgment about when it's worth the tokens."
        )
        return {
            **state,
            "decision": "act",
            "act_reason": f"interactive {source}: {combined[:60]}",
            "act_prompt": prompt,
            "act_model": select_cognitive_mode("george_task", daily_pct, interactive_tier),
        }

    # Hard stops
    if state["paused"]:
        log.info("DECIDE → pause (.tam-pause exists)")
        return {**state, "decision": "pause", "act_reason": "paused"}

    if is_quiet_hours(state["hour"]):
        log.debug(f"DECIDE → wait (quiet hours, hour={state['hour']})")
        return {**state, "decision": "wait", "act_reason": "quiet hours"}

    # Budget check
    budget = check_budget()
    if not budget.get("allowed", True):
        log.info(f"DECIDE → wait (budget: {budget.get('reason', '?')})")
        return {**state, "decision": "wait", "act_reason": f"budget: {budget.get('reason')}"}

    daily_pct = budget.get("daily_pct", 0)
    weekly_pct = budget.get("weekly_pct", 0)
    # Autonomous work: gate on max(daily, weekly) for full picture
    budget_tier = classify_budget(daily_pct, weekly_pct)
    if budget_tier in ("suspended", "hard_stop"):
        log.info(
            f"DECIDE → wait (budget {budget_tier}: daily={daily_pct:.0f}%, weekly={weekly_pct:.0f}%)"
        )
        return {
            **state,
            "decision": "wait",
            "act_reason": f"budget {budget_tier} (daily={daily_pct:.0f}%, weekly={weekly_pct:.0f}%)",
        }
    if budget_tier == "minimal":
        log.info(f"DECIDE: budget minimal — autonomous work forced to haiku")

    # Minimum gap between ACT calls
    last_act = state.get("last_act_timestamp", 0)
    elapsed = time.time() - last_act
    if elapsed < ACT_MIN_GAP_SECONDS:
        remaining = int(ACT_MIN_GAP_SECONDS - elapsed)
        log.debug(f"DECIDE → wait (min gap: {remaining}s remaining)")
        return {**state, "decision": "wait", "act_reason": f"min gap ({remaining}s)"}

    # George's tasks take priority — Deliberate mode (Opus, or downshifted)
    if state["has_queued_tasks"]:
        task = state["queued_tasks"][0]
        log.info(f"DECIDE → act (task queue: {task[:60]})")
        prompt = (
            "You are Tam. Read BOOT.md and follow the wake-up sequence. "
            "Check the Task Queue in /home/aldric/vaults/tam/State/Task Queue.md — "
            "work on the first queued task. "
            "If something needs George's attention, start your final response with NOTIFY: and a short message. "
            "IMPORTANT: Always end your run with a text summary of what you did."
        )
        return {
            **state,
            "decision": "act",
            "act_reason": f"task queue: {task[:60]}",
            "act_prompt": prompt,
            "act_model": select_cognitive_mode("george_task", daily_pct, budget_tier),
        }

    # Self-directed work from priorities — Flow mode (Sonnet, or downshifted)
    if state["has_active_priority"]:
        priority = state["active_priority"]
        log.info(f"DECIDE → act (active priority: {priority[:60]})")
        prompt = (
            "You are Tam. Read BOOT.md and follow the wake-up sequence. "
            f"Your current self-directed priority is: {priority}. "
            "Work on this now. Update Priorities.md when done or if you need to defer. "
            "IMPORTANT: Always end your run with a text summary of what you did."
        )
        return {
            **state,
            "decision": "act",
            "act_reason": f"active priority: {priority[:60]}",
            "act_prompt": prompt,
            "act_model": select_cognitive_mode("priority", daily_pct, budget_tier),
        }

    # Promote first queued priority if nothing active
    if state["queued_priorities"]:
        # Nothing active — promote the first queued item and act on it next cycle
        # (We update Priorities.md directly here rather than spawning a full session)
        next_priority = state["queued_priorities"][0]
        log.info(f"DECIDE → wait (promoting priority: {next_priority[:60]})")
        _promote_priority(next_priority)
        return {**state, "decision": "wait", "act_reason": "promoted next priority — act next cycle"}

    # Nothing to do
    log.debug("DECIDE → wait (nothing to do)")
    return {**state, "decision": "wait", "act_reason": "nothing to do"}


def _promote_priority(priority_text: str) -> None:
    """Move a priority from Queued to Active in Priorities.md."""
    if not PRIORITIES_FILE.exists():
        return
    content = PRIORITIES_FILE.read_text()

    line_to_promote = f"- [ ] {priority_text}"

    # Remove from Queued (and any duplicate Active entries from prior bugs)
    # First, strip all instances of this line
    content = content.replace(line_to_promote + "\n", "")

    # Inject into Active section — replace the placeholder or insert after header
    if "(empty" in content or re.search(r"## Active\s*\n\s*\n## ", content):
        # Empty active section — replace placeholder
        content = re.sub(
            r"(## Active\s*\n)(\(empty[^\n]*\)\s*\n)?",
            f"\\1{line_to_promote}\n",
            content
        )
    else:
        # Active section has items — insert after header
        content = re.sub(
            r"(## Active\s*\n)",
            f"\\1{line_to_promote}\n",
            content,
            count=1
        )

    PRIORITIES_FILE.write_text(content)
    log.info(f"Promoted to active: {priority_text[:60]}")


def act(state: SupervisorState) -> SupervisorState:
    """ACT: Spawn a Claude Code session (or local LLM fallback if enabled)."""
    log.info(f"ACT [{state['act_model']}]: {state['act_reason']}")

    local_mode = os.environ.get("TAM_LOCAL_MODE", False)
    if local_mode:
        system_path = TAM_HOME / "docs" / "SOUL.md"
        soul_text = ""
        if system_path.exists():
            soul_text = system_path.read_text()

        result = run_local(
            prompt=state["act_prompt"],
            model=state["act_model"],
            system_prompt=soul_text,
        )
    else:
        result = run_claude(state["act_prompt"], model=state["act_model"])

    log.info(
        f"ACT complete: cost=${result['cost']:.4f}, "
        f"session={result['session_id'][:8] if result['session_id'] else 'none'}"
    )

    return {
        **state,
        "act_result": result["result"],
        "act_cost": result["cost"],
        "act_session_id": result["session_id"],
        "last_act_timestamp": time.time(),
    }


def reflect(state: SupervisorState) -> SupervisorState:
    """REFLECT: Note what happened. Update STATE.md and route responses back."""
    log.debug("REFLECT")

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = (
        f"\n## Supervisor act — {now}\n"
        f"- Reason: {state['act_reason']}\n"
        f"- Model: {state['act_model']}\n"
        f"- Cost: ${state['act_cost']:.4f}\n"
        f"- Result: {(state['act_result'] or '[empty]')[:200]}\n"
    )

    # Append to STATE.md (non-destructive — just adds a note)
    if STATE_FILE.exists():
        existing = STATE_FILE.read_text()
        if "## Supervisor" not in existing:
            STATE_FILE.write_text(existing + entry)
        else:
            # Replace previous supervisor section
            STATE_FILE.write_text(
                re.sub(r"\n## Supervisor act.*", entry, existing, flags=re.DOTALL)
            )

    # Route response back to the originating channel (web, discord, etc.)
    batch_id = state.get("stimulus_batch_id", "")
    channel = state.get("stimulus_response_channel", "")
    if response_writer is not None and batch_id and channel:
        result_text = state.get("act_result", "") or "[no response]"
        metadata = {
            "model": state.get("act_model", ""),
            "cost": state.get("act_cost", 0),
            "session_id": state.get("act_session_id", ""),
        }
        updated = response_writer.complete_by_channel(channel, result_text, metadata)
        if updated:
            log.info(f"REFLECT: routed response to channel={channel} ({updated} responses)")
        else:
            log.debug(f"REFLECT: no pending responses for channel={channel}")

    log.debug("REFLECT complete")
    return state


def wait(state: SupervisorState) -> SupervisorState:
    """WAIT: Sleep SENSE_INTERVAL_SECONDS before next SENSE cycle."""
    log.debug(f"WAIT {SENSE_INTERVAL_SECONDS}s ({state['act_reason']})")
    time.sleep(SENSE_INTERVAL_SECONDS)
    return state


# ── Routing ───────────────────────────────────────────────────────────────────

def route_decide(state: SupervisorState) -> str:
    decision = state.get("decision", "wait")
    if decision == "act":
        return "act"
    elif decision == "pause":
        return END
    else:
        return "wait"


def route_after_reflect(state: SupervisorState) -> str:
    return "sense"


def route_after_wait(state: SupervisorState) -> str:
    return "sense"


# ── Build the graph ───────────────────────────────────────────────────────────

def build_graph(checkpointer=None):
    builder = StateGraph(SupervisorState)

    builder.add_node("sense", sense)
    builder.add_node("decide", decide)
    builder.add_node("act", act)
    builder.add_node("reflect", reflect)
    builder.add_node("wait", wait)

    builder.set_entry_point("sense")
    builder.add_edge("sense", "decide")
    builder.add_conditional_edges("decide", route_decide, {"act": "act", "wait": "wait", END: END})
    builder.add_edge("act", "reflect")
    builder.add_edge("reflect", "sense")
    builder.add_edge("wait", "sense")

    return builder.compile(checkpointer=checkpointer)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tam's autonomous supervisor")
    parser.add_argument("--loop", action="store_true", help="Run continuous loop (default: one cycle)")
    parser.add_argument("--dry-run", action="store_true", help="Sense + decide only, don't act")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    graph = build_graph()

    # Initialise stimulus pipeline
    global stimulus_processor, response_writer
    stimulus_processor = StimulusProcessor(STIMULI_DB)
    response_writer = ResponseWriter(STIMULI_DB)
    log.info(f"Stimulus pipeline: {STIMULI_DB}")

    initial_state: SupervisorState = {
        "has_queued_tasks": False,
        "queued_tasks": [],
        "has_active_priority": False,
        "active_priority": "",
        "queued_priorities": [],
        "paused": False,
        "hour": 0,
        "last_act_timestamp": 0.0,
        "decision": "wait",
        "act_reason": "",
        "act_prompt": "",
        "act_model": "sonnet",
        "act_result": "",
        "act_cost": 0.0,
        "act_session_id": "",
        "stimulus_batch_id": "",
        "stimulus_response_channel": "",
        "has_interactive_stimulus": False,
        "iteration": 0,
    }

    log.info("Tam supervisor starting")
    log.info(f"Checkpoint DB: {CHECKPOINT_DB}")

    if args.dry_run:
        # Sense + decide only — no checkpoint needed
        state = sense(initial_state)
        state = decide(state)
        print(f"Decision: {state['decision']}")
        print(f"Reason: {state['act_reason']}")
        if state['decision'] == 'act':
            print(f"Would invoke ({state['act_model']}): {state['act_prompt'][:200]}")
        return

    # ── Open checkpoint DB ───────────────────────────────────────────────────
    # SqliteSaver.from_conn_string is a context manager; use it for --loop or
    # single-cycle. State persists across restarts.
    with SqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as checkpointer:

        # Restore last_act_timestamp from checkpoint if available
        stored = read_checkpoint(checkpointer)
        if stored:
            initial_state = {**initial_state, **{
                k: stored[k] for k in ("last_act_timestamp", "iteration")
                if k in stored
            }}
            log.info(
                f"Resumed from checkpoint: iteration={initial_state['iteration']}, "
                f"last_act={datetime.fromtimestamp(initial_state['last_act_timestamp']).strftime('%H:%M:%S') if initial_state['last_act_timestamp'] else 'never'}"
            )

        if args.loop:
            # Continuous loop — intended for systemd service
            log.info("Running as continuous loop (Ctrl+C to stop)")
            state = initial_state
            try:
                while True:
                    state = sense(state)
                    state = decide(state)
                    decision = state.get("decision", "wait")

                    if decision == "pause":
                        log.info("Paused (.tam-pause exists). Sleeping 60s.")
                        time.sleep(60)
                        continue

                    if decision == "act":
                        state = act(state)
                        state = reflect(state)
                        write_checkpoint(checkpointer, state)
                    else:
                        state = wait(state)

            except KeyboardInterrupt:
                log.info("Supervisor stopped by user")
        else:
            # Single cycle — sense + decide + optional act, then exit.
            # Does not loop. Useful for manual testing or cron-like invocation.
            log.info("Running single cycle")
            state = sense(initial_state)
            state = decide(state)
            decision = state.get("decision", "wait")

            if decision == "act":
                state = act(state)
                state = reflect(state)
                # Persist updated last_act_timestamp to checkpoint
                write_checkpoint(checkpointer, state)
                print(f"Acted: {state['act_reason']}")
                print(f"Cost: ${state.get('act_cost', 0):.4f}")
                print(f"Result snippet: {(state.get('act_result') or '')[:300]}")
            else:
                print(f"No action: {state['act_reason']}")


if __name__ == "__main__":
    main()
