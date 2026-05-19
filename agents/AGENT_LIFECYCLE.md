# Agent lifecycle — add, split, delete

A trading desk agent on this codebase touches **18 surfaces** that all
have to stay consistent. If a single one drifts (a stale prompt in
`mike-morning.md`, a yaml block that was never deleted, a Telegram
caption template still saying "TITAN"), the agent leaks back into the
running system in a confusing half-state. This file is the checklist.

**Real example of what goes wrong without this checklist:** Titan was
decommissioned 2026-04 and replaced by `energy` + `commodity`. We did
the migration in a hurry — the persona YAML survived, three skill files
survived, the Telegram chart caption template in `commodity-evening.md`
+ `energy-evening.md` still said "TITAN | …", `mike-morning.md` still
named Titan in its sector-cluster brief, `mike-midday.md` still listed
Titan as one of "your four traders", and `db/schema.py` was carrying a
runtime SQL split (`_TITAN_TO_ENERGY` / `_TITAN_TO_COMMODITY`) to
re-route the old YAML seed at boot. Cleaning it up properly took the
2026-05-19 pass and would have been 15 minutes if we'd had this
checklist on day 1.

---

## Triggers

Use this protocol for any of:

- **Add** a new agent (new sector specialist, new role).
- **Split** an agent (one agent's universe carved into N successor
  agents — what happened to titan).
- **Delete** an agent (decommission, no successor).
- **Rename** an agent (`semi` → `fab` + `fabless` is split, but a pure
  rename — same scope, new name — is its own case).

The deletion path is the inverse of the add path. The split path is
**add successor(s) first, then delete predecessor** — never the other
way around (you'd leak in-flight conviction rows referencing the
predecessor while the allocator still loads them).

---

## The 18 surfaces

Grouped by where they live. **Every surface listed here was a real
gotcha on the titan cleanup** — if you skip even one, the agent leaks.

### Source of truth (must be edited first)

1. **`agents/sector_map.yaml`** — the watchlist seed. Add a new
   top-level block; or move symbols between blocks for a split;
   or delete the block for decommission. Add a `# NOTE: ...` comment
   when deleting so future readers know why the position is empty.

2. **`agents/<agent>.yaml`** — the persona file. Contains
   `name`/`description`/`enabled`/`system_prompt`. On delete, just
   `rm` the file. The agent registry (`agent/agent_registry.py`)
   reads `agents/*.yaml` at import time, so the registry refreshes
   automatically on next process boot — **do NOT also edit the
   registry's hard-coded fallback list (there isn't one)**.

3. **`agents/<agent>/`** directory — model code, persona helpers,
   anything per-agent. `rm -rf` on delete. On add, scaffold with at
   least an `__init__.py` and a `models/` dir per `MODEL_CONTRACT.md`.

### Pipeline routing

4. **`meta_agent/queue_primer.py:PIPELINE_SECTORS`** — the 11-sector
   list the hourly orchestrator fans out to. Add/remove the agent
   name. Order is the canonical lexicographic.

5. **`scripts/run_evening_orchestrator.sh:PIPELINE_SECTORS`** — shell
   array, same list as #4 in evening orchestrator. Keep in lockstep
   with #4 (a future refactor could make this read from #4 directly).

6. **`agent/agent_registry.py`** — verify by importing + listing.
   No edit usually needed (it reads `agents/*.yaml`); but if there
   are any hard-coded references / fallback lists, update them.

### Skill files (`.claude/commands/`)

7. **`.claude/commands/<agent>-review.md`** — hourly conviction-publish
   skill. On add, copy `atlas-review.md` as a template and customize.
   On delete, `rm` the file.

8. **`.claude/commands/<agent>-evening.md`** — end-of-day attribution.
   Same pattern; check the Telegram caption template at STEP 6 — it
   carries the agent's display name and must match #2's description.

9. **`.claude/commands/<agent>-respond.md`** — on-demand Q&A.
   Same pattern.

10. **`.claude/commands/<agent>-model-tune.md`** — weekly model
    portfolio tune. Optional (newer agents may not have one).

### Cross-agent docs that name agents inline

These are easy to miss because they don't reference the agent
programmatically — just in prose. Grep for the agent name in:

11. **`.claude/commands/mike-morning.md`** — sector-cluster brief
    + thesis summary template. Both mention each agent by name.

12. **`.claude/commands/mike-midday.md`** — trader list at top,
    Telegram template P&L lines. Update both.

13. **`agents/atlas.yaml`** and **`agents/mike.yaml`** — these
    personas reference other agents by name in their system_prompt
    (e.g., atlas says "leave shorts to Titan" — was wrong even
    before titan was deleted, just less visibly so).

14. **`.claude/commands/cassidy-evening.md`** — Cassidy's risk
    review iterates over all agents. Check her per-agent loop.

### Operator-facing UI / observability

15. **`reporting/agent_chart.py`** — the labels dict at the top.
    Used for Telegram chart captions on evening attribution slides.

16. **`obs/dashboard.py:AGENTS_ORDER`** — Streamlit grid order +
    labels (`AGENT_TAGLINES`). Both lists need editing.

17. **`scripts/plot_agent_forecast.py`** — has a per-agent model
    registry (`MODEL_CONFIG`) AND per-agent `_commentary_<agent>`
    functions. Both must be in sync.

### Database state

18. **DB cleanup** — when **deleting** an agent, decide per-table
    what to do with historical rows:

    ```python
    # Delete (no audit value, will confuse reporting):
    DELETE FROM kill_switch     WHERE agent_name='<agent>';
    DELETE FROM agent_watchlist WHERE agent_name='<agent>';
    DELETE FROM agent_state     WHERE agent_name='<agent>';  # stale snapshots

    # Keep (audit trail, evening reviews look back on history):
    -- agent_conviction         (active rows expire on their own; no harm)
    -- agent_forecast           (same)
    -- agent_thesis             (journal — historical reasoning)
    -- agent_ledger             (P&L event log)
    -- agent_evening_digests    (per-day notes)
    -- sector_story             (weekly archive)
    ```

    `agent_inbox`, `agent_job` — let queued rows drain naturally; once
    enabled=false on the agent (or the agent isn't in
    PIPELINE_SECTORS), no new rows land.

---

## Order of operations (the safe one)

### Adding an agent

1. Pick the name (lowercase, one word — see `docs/ADDING_AN_AGENT.md`).
2. Add the block in `agents/sector_map.yaml` (surface #1).
3. Add the persona in `agents/<agent>.yaml` (surface #2).
4. Scaffold `agents/<agent>/` (surface #3).
5. Add to `PIPELINE_SECTORS` in BOTH `meta_agent/queue_primer.py` (#4)
   and `scripts/run_evening_orchestrator.sh` (#5).
6. Add the 4 skill files (#7-#10) — review, evening, respond,
   model-tune.
7. Update the cross-agent docs (#11-#14) so other agents and Mike
   know about the new sector.
8. Update operator-facing dashboards (#15-#17).
9. Restart `trading-queue-worker@*`, `trading-concierge`,
   `trading-dashboard`. The hourly orchestrator picks it up at the
   next top-of-hour.
10. Verify: `mcp__ibkr-trading__get_agent_list` shows the new agent;
    `mcp__ibkr-trading__get_kill_switch_status` partitions correctly;
    `mcp__ibkr-trading__prime_sector_queues` includes it in the
    per_agent fan-out result.

### Splitting an agent (e.g. one → two specialists)

1. **First, add the successors** (full add-an-agent flow above) so
   they're operational before the predecessor goes away.
2. Run the successors alongside the predecessor for at least one
   full hourly cycle to verify their universes + convictions land
   correctly.
3. Then do the delete-an-agent flow on the predecessor (below).
4. **Do NOT add a runtime "split" SQL/Python shim** to translate the
   predecessor's symbols into the successors' universes. That was
   the mistake on titan — the shim survived the migration and lived
   in `db/schema.py` for a year before getting cleaned up. Put the
   symbols in the **YAML** blocks of the successors directly; never
   do runtime translation.

### Deleting an agent

1. Confirm the agent has no active convictions: `SELECT COUNT(*)
   FROM agent_conviction WHERE agent_name='<agent>' AND expires_at
   > NOW()`. If non-zero, wait for them to expire or call
   `clear_agent_convictions('<agent>')`.
2. Confirm no positions in the active stack reference this agent's
   votes (check `allocation_decision.contributors` JSONB on recent
   rows). The advisory lock + sequential allocator mean
   you can't race a delete with a live rebalance.
3. Delete in this order:
   - `rm agents/<agent>.yaml` (#2)
   - `rm -rf agents/<agent>/` (#3)
   - `rm .claude/commands/<agent>-*.md` (#7-#10)
   - Delete the block in `agents/sector_map.yaml` (#1) OR replace
     with a `# NOTE: decommissioned YYYY-MM-DD` comment for future
     archaeologists
   - Remove from `PIPELINE_SECTORS` (#4, #5)
   - Edit cross-agent docs (#11-#14)
   - Edit operator UI (#15-#17)
   - DB cleanup (#18) — delete from kill_switch, agent_watchlist,
     agent_state
4. Verify with `grep -rln -i "<agent>"` excluding `.venv` and
   `__pycache__` and `worktrees`. Expected hits: at most a few
   NOTE/CHANGELOG comments mentioning the decommission.
5. Restart `trading-queue-worker@*`, `trading-concierge`,
   `trading-dashboard`.
6. Run `pytest tests/` — all green.

---

## Pre-flight grep (run this BEFORE you start)

```bash
cd "/home/tianyizhang/opus trading"
grep -rln -i "<agent>" \
  --include="*.py" --include="*.yaml" --include="*.yml" \
  --include="*.md" --include="*.sh" --include="*.json" \
  --include="*.sql" 2>/dev/null \
  | grep -v ".venv\|__pycache__\|worktrees\|.git/"
```

This finds every file that names the agent. The 18 surfaces above
cover almost everything, but the grep catches the rest. **Do not
trust this file's list alone** — grep first.

---

## Post-flight grep (run this AFTER you're done)

Same command. The output should be:
- For **delete**: ≤3 hits, all in NOTE/comments explaining the
  decommission.
- For **add**: the new agent's hits across the 18 surfaces and
  wherever you intentionally added prose mentions.
- For **split**: same as add for the successors + same as delete
  for the predecessor.

If there are surprise hits, fix them before considering the lifecycle
operation complete.

---

## Note to future Claude agents

When you do one of these lifecycle operations:

1. **Read this file first.** Do not freelance the surface list — it's
   here because each item has bitten us.
2. **Use the pre-flight grep to discover the actual scope** — codebases
   drift. New scripts, new docs, new skill files appear. Trust the
   grep output over this file's enumeration when they disagree.
3. **Land surface #4 (PIPELINE_SECTORS) last for ADD, first for DELETE.**
   This is the runtime hot-loop's source of truth — flipping it
   atomically swaps the agent in or out. Wait until everything else
   is ready (for add) or already-cleaned (for delete) before touching
   it.
4. **Append your operation to this file's CHANGELOG below.** Future
   readers (including the next Claude) benefit from knowing what got
   added/split/deleted and what was learned. Format:

   ```
   - YYYY-MM-DD <add|split|delete> <agent>: <one-line reason>.
     Surprises encountered: <bullet list, or "none">.
   ```
5. **If you discover a NEW surface not listed in #1–#18 above,
   ADD IT** — both to the enumeration here and to your CHANGELOG
   entry. The whole point of this file is that the next iteration
   gets a tighter list than the last one.

---

## CHANGELOG

- **2026-05-19 — delete titan**: persona, models, skill files,
  YAML block, agent_state row, and ~12 cross-references in mike
  prompts / dashboards / docs / scripts. The runtime split shim in
  `db/schema.py` + `scripts/ingest_news.py` was also removed (the
  YAML now seeds energy + commodity directly, no translation
  needed). Surprises: (a) `agents/atlas.yaml` still referenced
  Titan in its persona ("leave shorts to Titan") — fixed by aligning
  Atlas's bearish-routing language with the desk-wide inverse-ETF
  convention; (b) `agents/mike.yaml` named Titan in its trader list —
  rewrote to enumerate the 11 sector agents; (c) two Telegram caption
  templates in `commodity-evening.md` + `energy-evening.md` were
  still using the literal string "TITAN | …" — these would have
  shown up in production Telegrams every evening as "TITAN" labels
  on Commodity/Energy slides. Caught only by grep, not by docstring
  review.

- **2026-04-26 — split titan → energy + commodity**: did surfaces
  #4/#5 (PIPELINE_SECTORS) and added the two new yaml blocks. Missed
  surfaces #1 (kept titan: block in sector_map.yaml), #2/#3 (kept
  persona + dir), #7/#8 (kept review + evening skills), #11-#14
  (kept inline references in mike + atlas + cassidy prompts), and
  #15-#17 (kept titan labels in dashboards / charts). The runtime
  split shim (`_TITAN_TO_ENERGY` / `_TITAN_TO_COMMODITY` in
  db/schema.py and ingest_news.py) was added as a band-aid because
  surface #1 wasn't done. This file is the post-mortem.
