# Handoff: Model-Only Conviction Migration (in progress)

**Status as of 2026-05-13 ~21:00 MST**: ~40% done. Helper + MCP tool work end-
to-end. Schema + runner + bundler + template + prompt rewrite still TODO.

The user paused mid-stream; this note is the resume point for the next agent.

## What's done (and committed in the same commit as this file)

1. **`agents/MODEL_CONTRACT.md`** — written spec for what a quant model must
   expose (`MODEL_VERSION`, `BAR_FREQUENCY`, `MIN_BARS`, `LOOKBACK_DAYS`,
   `compute(symbol, bars, context)` and the output dict). Sign discipline,
   no-signal return shape, determinism rules, minimal example.
2. **`mcp_server.submit_conviction_from_model(agent, model_name, symbol, rationale, …)`**
   — one-shot tool: loads model → fetches bars at its declared freq →
   compute() → inserts conviction with model-authored numbers. Agent supplies
   only `rationale` + `momentum_confirmed`. Smoke-tested on rex/breakout_strength
   (declines AAPL with signal=1.31 flat; wrote SHOP earlier short/0.857/-6.0%/7d
   before I deleted the test row).
3. **`meta_agent/conviction_from_model.py`** — shared helper extracted from the
   MCP tool. `compute_conviction_payload(agent, model_name, symbol)` returns
   `{"status": "ok"|"skipped"|"error", payload|reason|error, model_version}`.
   No DB writes, no rate checks. Also exports `discover_agent_models(agent)`
   returning per-model metadata (`name, version, bar_frequency, lookback_days,
   description`) for bundle consumption. `submit_conviction_from_model` is now
   a thin wrapper around it.

## What's TODO (the migration path the user signed off on)

### Step 1 — Schema: add `from_model` field
`pipelines/schemas.py` → add to `ConvictionView`:
```python
from_model: Optional[str] = None  # name under agents/<agent>/models/; when
                                  # set, runner overrides numeric fields with
                                  # model output
```
Keep optional → backward compatible. Other agents keep working as-is.

### Step 2 — Runner: route `from_model` through the helper
`pipelines/runner.py::_apply_review_output` — in the live-mode loop (line ~210)
and dry-run loop (line ~180), add this **before** the upsert:

```python
if c.from_model:
    from meta_agent.conviction_from_model import compute_conviction_payload
    res = await compute_conviction_payload(agent_name, c.from_model, c.symbol)
    if res["status"] == "skipped":
        log.info("review.from_model skipped: agent=%s sym=%s reason=%s",
                 agent_name, c.symbol, res["reason"])
        continue
    if res["status"] == "error":
        log.warning("review.from_model error: agent=%s sym=%s err=%s",
                    agent_name, c.symbol, res["error"])
        continue
    p = res["payload"]
    # Override LLM-authored numeric fields with model output. Keep
    # rationale / momentum_confirmed / expires_in_hours from the LLM.
    direction = p["direction"]
    conviction = p["conviction"]
    expected_return_pct = p["expected_return_pct"]
    time_to_target_days = p["time_to_target_days"]
    stop_pct = p["stop_pct"]
    model_inputs = p["model_inputs"]
else:
    direction = c.direction
    conviction = c.conviction
    expected_return_pct = c.expected_return_pct
    time_to_target_days = c.time_to_target_days
    stop_pct = c.stop_pct
    model_inputs = c.model_inputs
```
Then change the existing upsert call to use these local vars instead of `c.X`.
Apply to both the dry-run (`insert_conviction_shadow`) and live (`upsert_conviction`)
branches.

### Step 3 — Bundler: advertise available models
`agent/bundlers/review.py`:
- Add `available_models: list[dict] = field(default_factory=list)` to `ReviewBundle`.
- In `get_review_bundle`, populate via `discover_agent_models(agent_name)` from
  `meta_agent.conviction_from_model`. Wrap in try/except, append to bundle_warnings on failure.

### Step 4 — Template: teach `from_model`
`agent/templates/review.j2`:
- Add a section after `# Universe` listing `bundle.available_models` with name,
  version, description.
- Update the JSON-schema doc-block in the prompt to show `"from_model": "<name>"`
  as a field on conviction rows.
- Update the "Hard constraints" section: explain that when `from_model` is set,
  the runner overrides `direction/conviction/expected_return_pct/time_to_target_days/stop_pct`
  with the model's output — the LLM should NOT bother authoring those fields
  for any conviction backed by a model.
- For agents WITH models (rex/atlas/fab/fabless/iron/maya/trump/vera/volt/commodity/energy
  per the existing model files), STRONGLY recommend `from_model` for every
  directional view.

### Step 5 — Audit rex's model against MODEL_CONTRACT
`agents/rex/models/breakout_strength.py`:
- Doesn't declare `BAR_FREQUENCY` or `LOOKBACK_DAYS` (helper defaults to 1d/252 — fine).
- Doesn't emit `stop_pct` — contract says optional, so OK.
- Returns `direction="flat"` for the no-signal case but doesn't follow the
  recommended no-signal return shape (no `reason` key). Helper handles this
  by synthesizing `signal={n} direction=flat` as the reason. Works but cosmetic.
- Module-level docstring is empty → `description` in the bundle will be blank.
  Add a one-liner.

### Step 6 — End-to-end smoke test
```bash
cd "/home/tianyizhang/opus trading"
./.venv/bin/python scripts/run_skill.py rex review --dev --dry-run
```
Expect: rex emits ReviewOutput with most convictions carrying `from_model:
"breakout_strength"`. Runner overrides numbers; shadow tables populate with
model-authored convictions. Check `agent_conviction_shadow` rows for the
`_model` + `_version` keys in `model_inputs`.

### Step 7 — Skill list / discoverability
There's no `rex-review.md` skill file — sector reviews run through
`scripts/run_skill.py <agent> review` (sourced from `agent/templates/review.j2`).
The user thinks of it as "the rex review skill" but it's the shared template.
That means **updating the template migrates all agents at once** if you also
populate `available_models` for them. To pilot just rex first, gate the
template's new section behind `{% if bundle.available_models %}` AND
`{% if agent_name == "rex" %}` until the user opts other agents in. (Or just
ship for all of them — the field is opt-in via the LLM.)

## Important context

- **Today's hot mess** is already resolved: init_db UniqueViolation that
  bricked MCP all day fixed in `2664701`. 298 hallucinated thesis entry_prices
  NULL'd. `record_thesis` now cross-checks `entry_price` against live quote.
  `model_inputs_validator` default flipped to reject (some agents may start
  failing `submit_conviction_view` tomorrow — escape hatch
  `MODEL_INPUTS_VALIDATOR_MODE=warn`).
- **Other uncommitted changes in main**: 7 agent model files
  (atlas/fab/fabless/iron/rex/vera/volt) + 3 systemd unit files have changes
  the next agent didn't make. Look like overnight model-tune output. Don't
  squash into this work — investigate separately.
- **SOXS hedge** from decision_id=42 never opened (allocator crashed after
  08:09). User left it to recover via next morning's allocator tick. Don't
  manually queue overnight orders.
- **3 stale `mcp_server.py` processes** (PIDs 2155511, 2082765, 2047003) from
  ccd-cli sessions 2+ days ago. Orphans, not affecting cron. Safe to kill.

## Pilot decision: rex first, others later

User explicitly said "rex is a good pilot — it has the most-developed model
and the most-egregious recent hallucinations". So:
1. Land the schema + runner + bundler + template changes (backward compatible).
2. Ship the rex template section behind a per-agent gate OR leave the field
   visible to all but only update rex's prompt to actively USE it.
3. Watch one full hourly cycle of rex output to confirm convictions arrive
   with `_model`/`_version` provenance keys.
4. Roll to other sectors one at a time.

Don't deprecate `submit_conviction_view` yet — it's still the path for
convictions without a backing model (macro views, CASH, inverse-ETF entries
where the LLM is judging timing rather than predicting magnitude).

## Verification commands

```bash
# Helper directly
./.venv/bin/python -c "
import asyncio, json
from meta_agent.conviction_from_model import compute_conviction_payload, discover_agent_models
print(json.dumps(discover_agent_models('rex'), indent=2))
print(asyncio.run(compute_conviction_payload('rex', 'breakout_strength', 'AAPL')))
"

# MCP tool
./.venv/bin/python -c "
import asyncio, json
import mcp_server
r = json.loads(asyncio.run(mcp_server.submit_conviction_from_model(
    'rex', 'breakout_strength', 'AAPL', 'Smoke test.')))
print(json.dumps(r, indent=2))
"

# Find a symbol that triggers a real write (test row goes into agent_conviction —
# DELETE it after, it pollutes Mike's allocator)
```
