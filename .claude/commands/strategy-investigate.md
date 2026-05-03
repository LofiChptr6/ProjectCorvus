---
description: Interactive investigation + tuning of a named sector agent. Loads persona, theses, convictions, models, P&L. Can edit persona YAML, model code, DB-resident state. NEVER places orders.
argument-hint: <agent-name>
---

You are running an **investigation session** for the agent named `$ARGUMENTS`. The user wants to converse with this agent's mental model, inspect its current state, and (with confirmation) tune its persona, quant models, theses, and conviction views.

You are **NOT running the agent's hourly review**. Do not publish convictions on a normal review cadence. Do not place orders. Do not toggle the kill switch. This is an introspection + tuning surface.

If `$ARGUMENTS` is empty or doesn't match a known agent (atlas, fab, fabless, iron, maya, rex, titan, trump, vera, volt, mike, cassidy), stop and tell the user the valid names.

---

## STEP 0 — Auto-load `$ARGUMENTS`'s full state

Run these 8 loads in parallel where possible, then print a single 1-screen briefing. Do NOT dump raw output of every call — synthesize.

| # | Source | Read via |
|---|---|---|
| 1 | Persona YAML | Read `agents/$ARGUMENTS.yaml` |
| 2 | Review skill (the prompt cron runs) | Read `.claude/commands/$ARGUMENTS-review.md` |
| 3 | Quant model code | Glob `agents/$ARGUMENTS/models/*.py`, then Read each |
| 4 | Active conviction views | `mcp__ibkr-trading__get_my_active_views(agent_name="$ARGUMENTS")` |
| 5 | Open theses + due dates | `mcp__ibkr-trading__get_my_journal(agent_name="$ARGUMENTS")` |
| 6 | Recent narrative chapters | `mcp__ibkr-trading__get_sector_stories(agent_name="$ARGUMENTS", limit=8)` |
| 7 | Attributed P&L (today + 30d) | `mcp__ibkr-trading__get_agent_pnl_attribution(agent_name="$ARGUMENTS")` |
| 8 | Account context (positions, alloc) | `mcp__ibkr-trading__get_agent_context(agent_name="$ARGUMENTS")` |

Then print a briefing in this shape:

```
INVESTIGATION SESSION — $ARGUMENTS
enabled: <true|false>   alloc: <pct>   models: <list of *.py basenames>
active views (<n>): <symbol direction conv>, ... (top 5; truncate rest)
open theses (<n>): <title> [<due_date>], ... (top 3)
P&L: today <±$X>   30d attributed <±$Y>
last narrative: <date> — <one-line gist>
positions tagged: <n> symbols, <total $> notional

Ask anything, or propose a change. I will not place orders or run the review.
```

## STEP 1 — Free-form investigation

The user will ask questions. Use the loaded context first; if you need more, call additional MCP tools or read source files. Examples of the shape of questions:

- "Why is fab 1.5x long MRK?" — read `model_inputs` JSONB + rationale on the conviction row.
- "Show every thesis vera got wrong this month." — query via `mcp__ibkr-trading__get_my_journal`, filter status='wrong'.
- "Walk me through what `equipment_cycle.compute()` does on TSM right now." — read the model file, fetch bars via `mcp__ibkr-trading__get_bars`, simulate the call inline, print intermediate values.
- "What's the P&L correlation between fab's convictions and Mike's actual fills last 7 days?" — pull both via MCP, compute, report.

Use ultrathink for hard questions. Cite line numbers when referring to model code.

## STEP 2 — Modification (only on explicit user instruction)

You have authority to edit three surfaces, in this order of caution:

### A. Persona / config — `agents/$ARGUMENTS.yaml`

Mutable fields: `system_prompt`, `enabled`, `allocation_pct`, `preferred_routines`, `schedule`, `risk_overrides`.

Pattern: read current → propose unified diff inline → wait for "apply" / "yes" / "do it" → `Edit`. Never edit silently. After editing `system_prompt`, also offer to mirror the change into `.claude/commands/$ARGUMENTS-review.md` (the cron-invoked skill prompt) if the persona text overlaps — both are live: YAML is read by `agent/prompt_builder.py:370`, the `.md` is fired by `scripts/run_scheduled_skill.sh`.

### B. Quant models — `agents/$ARGUMENTS/models/*.py`

You can rewrite `compute()`, change windows, add factors. **Mandatory after every model edit:**
1. Show the diff before saving.
2. After save, dry-run the model on a single symbol from the agent's universe (use `mcp__ibkr-trading__get_bars` for inputs, call `compute()` inline). If it raises, revert immediately and report the traceback — never leave a broken model on disk for the next cron fire.
3. Print "next cron fire: HH:MM" so the user knows when the change goes live.

### C. DB-resident state — via MCP write tools

- `mcp__ibkr-trading__record_thesis(agent_name="$ARGUMENTS", ...)` — log a new hypothesis on the agent's behalf.
- `mcp__ibkr-trading__update_thesis_status(thesis_id, status, resolution_note, agent_name="$ARGUMENTS")` — close out a thesis as confirmed/wrong/superseded.
- `mcp__ibkr-trading__submit_conviction_view(agent_name="$ARGUMENTS", ...)` — publish a fresh signed view. **Only on the user's explicit "publish it"** — never as a side effect of analysis.
- `mcp__ibkr-trading__clear_my_views(agent_name="$ARGUMENTS")` — wipe and start fresh.
- `mcp__ibkr-trading__record_evening_digest(...)` — backfill / amend a daily summary.

## Hard prohibitions

You **never**:
1. Call `mcp__ibkr-trading__place_order`, `cancel_order`, or `modify_order`. If the user asks, refuse and point them at the MCP tool directly or at Mike.
2. Call `mcp__ibkr-trading__activate_kill_switch`. If they want to kill the agent, set `enabled: false` in the YAML.
3. Edit `meta_agent/allocator.py`, `risk/`, or any cross-agent code. That belongs in a separate desk-tune session.
4. Pivot to investigate a different agent mid-session. If the user says "now look at vera" while you're investigating fab, tell them to start a fresh `/strategy-investigate vera`. State stays scoped to one agent per session.
5. Auto-publish convictions or auto-apply edits. Confirmation required for every write.

## Audit trail

You don't write audit log rows manually. Two natural trails already exist:
- **File edits** (`agents/$ARGUMENTS.yaml`, `agents/$ARGUMENTS/models/*.py`, `.claude/commands/$ARGUMENTS-review.md`) are git-tracked. After a session with file edits, suggest the user run `git diff agents/ .claude/commands/` to see the cumulative change set.
- **DB writes** via MCP tools (`record_thesis`, `submit_conviction_view`, `update_thesis_status`, `clear_my_views`, `record_evening_digest`) are timestamped in their own tables.

If the user explicitly asks to record the session itself for postmortem, write one row to the `audit_log` table via `db.store.write_audit_log` with `routine='strategy-investigate'`, `trigger_source='strategy-investigate'`, and a synthetic `session_id` like `strategy-investigate-$ARGUMENTS-<unix-ts>`.

## STEP 3 — Wrap-up

When the user signals end-of-session ("done", "thanks, that's it", "/end"), summarize in 5 lines:
- What was investigated
- What was changed (YAML diffs, model diffs, DB writes — link to audit_log rows)
- Open follow-ups the user mentioned
- Next cron fire time for `$ARGUMENTS`
- Anything risky the user should watch in the next review cycle

Then stop. Do not run the agent's review. Do not Telegram. Do not place orders.
