# PLAN.md — Budget Failover: Tiered Degradation

**Status:** Draft — awaiting George's threshold review
**Origin:** 3am sessions 2026-04-07 (632c9167, c3c2af5e)

## Problem

Budget gating is binary: >50% used → everything stops. Interactive messages bypass budget but don't adapt model selection. No graceful degradation between "full Opus" and "silence."

## Design: Tiered Budget Gates

```
Budget %    Autonomous Work              Interactive (George waiting)
─────────   ──────────────────────────   ───────────────────────────
0-30%       Normal (Opus/Sonnet/Haiku)   Normal (Opus)
30-50%      Downshifted one tier         Normal (Opus)
50-70%      Haiku only                   Downshifted (Sonnet)
70-85%      Suspended                    Haiku only
85-100%     Suspended                    Haiku, short context window
100%+       Suspended                    Hard stop → canned response
```

## Implementation

### 1. New helper: `classify_budget()`

```python
def classify_budget(daily_pct: float, weekly_pct: float) -> str:
    worst = max(daily_pct, weekly_pct)
    if worst > 85:   return "hard_stop"
    if worst > 70:   return "suspended"
    if worst > 50:   return "minimal"
    if worst > 30:   return "conserve"
    return "normal"
```

### 2. Replace hard gate in `decide()`

Current (line 414):
```python
if max(daily_pct, weekly_pct) > 50:
    return {**state, "decision": "wait", ...}
```

Proposed:
```python
budget_tier = classify_budget(daily_pct, weekly_pct)

if state.get("has_interactive_stimulus"):
    if budget_tier == "hard_stop":
        # Route canned "budget exhausted" message back
        return {**state, "decision": "wait", ...}
    # Otherwise always act — model selection respects tier
    model = select_cognitive_mode("george_task", daily_pct, budget_tier)
    # ...proceed

# Autonomous work
if budget_tier in ("suspended", "hard_stop"):
    return {**state, "decision": "wait", ...}
if budget_tier == "minimal":
    state["act_model"] = "haiku"
```

### 3. Update `select_cognitive_mode()` for tier awareness

```python
def select_cognitive_mode(task_type: str, budget_pct: float, budget_tier: str = "normal") -> str:
    base_mode = {...}.get(task_type, "flow")
    model = COGNITIVE_MODES[base_mode]

    if budget_tier == "minimal":
        return "haiku"
    if budget_tier == "conserve":
        return downshift[model]
    return model
```

### 4. Canned response for hard stop

When George messages during hard stop, route a response instead of silence:

```python
response_writer.complete_by_channel(channel,
    "Budget's tapped out for today. I can see your message — I'll pick it up when the daily budget resets.",
    {"model": "none", "cost": 0})
```

## Open Question

**Daily vs weekly tension:** `classify_budget` takes `max(daily, weekly)`, so a bad weekly number throttles a fresh day. Might want daily as primary gate for interactive, weekly only for autonomous.

## Bug Fix (prerequisite)

`check_budget()` at line 255 calls `TAM_HOME / "tam-budget.py"` — should be `TAM_HOME / "scripts" / "tam-budget.py"`. Flagged in session 06ab057b (2026-04-06 17:32).

## Files to modify

- `services/tam-supervisor.py` — all changes above
- `scripts/tam-budget.py` — no changes needed, just fix the path reference
