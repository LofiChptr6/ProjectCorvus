# SOP — Adding a new sector agent

Standard procedure for adding a new sector agent to the desk. Follow in order.
The desk runs the **sector-shard architecture** (since 2026-04-26): sector
agents publish *signed conviction views* per symbol; **mike-allocator** reads
the consolidated stack, sizes the desk's actual positions, and trades. New
sector agents inherit this pattern — they do **not** trade directly.

Total time end-to-end: ~30–60 minutes including a smoke-test review.

---

## 0. Decide the agent

Answer these before touching any file:

- **Name.** Lowercase, one word, no underscores, no conflict with existing
  (`atlas`, `cassidy`, `commodity`, `energy`, `fab`, `fabless`, `iron`,
  `maya`, `mike`, `rex`, `trump`, `vera`, `volt`). Match the desk's
  character-name convention (or sector-name for the resource specialists).
- **Sector / responsibility.** What slice of the universe does the agent own?
  E.g., "global crypto + miners", "small-cap biotech".
  Avoid overlap >30% with any existing agent's universe — that creates
  duplicate convictions and clutters the allocator.
- **Universe.** 10–35 underlyings. Mix of single names + sector ETFs.
- **Bearish vehicles.** For each underlying, choose: `'inverse_etf:XYZ'`
  pointing at a **verified** entry from `agents/inverse_etf_map.yaml`,
  `'skip'` (no inverse available — bearish theses become CASH conviction),
  or omit (defaults to skip).
- **Enabled at launch?** Almost always `true`. Reserved-but-disabled is
  legal (e.g., agents/vera.yaml: `enabled: false` until earnings season).

---

## 1. Edit `agents/sector_map.yaml`

Add the agent's universe block under the `agents:` map. Use the existing
agents (atlas, fab, maya …) as templates. Quote tickers that YAML 1.1
coerces to non-strings (`"ON"`, `"NO"`, `"YES"`, etc. — bug fixed 2026-04-27;
quote them anyway). Example:

```yaml
agents:
  ...
  zara:
    description: Crypto-adjacent equities (miners, exchanges, stablecoin proxies)
    universe:
      MSTR:  {bearish_via: skip}                  # no verified inverse
      COIN:  {bearish_via: 'inverse_etf:CONL'}    # verify in inverse_etf_map.yaml first
      RIOT:  {bearish_via: skip}
      MARA:  {bearish_via: skip}
      IBIT:  {bearish_via: 'inverse_etf:BITI'}
      # …10–35 names total
```

**Validate**: `.venv/bin/python -c "import yaml; agents = yaml.safe_load(open('agents/sector_map.yaml'))['agents']; print({k: len(v.get('universe') or {}) for k, v in agents.items()})"` — every key should resolve to an integer count and `your-name` should be in the dict.

---

## 2. Create `agents/<name>.yaml`

Sector agents are 100 % conviction-publishers; they do not own a sleeve.
Set `allocation_pct: 0.0` so the legacy `risk.checks.allocation` gate doesn't
fire.

```yaml
name: zara
enabled: true
allocation_pct: 0.0   # sector conviction agent — Mike allocates capital
risk_overrides:        # optional: tighter caps than global risk config
  max_order_value: 5000
  max_position_pct: 0.10

system_prompt: |
  You are **Zara**, the crypto-adjacent equities analyst on a multi-agent
  quant desk. You do NOT place orders — you publish signed conviction views
  per symbol. Mike (the allocator) sizes the desk and trades.

  Use ultrathink. Reason in three frameworks before each call:
  fundamental (on-chain trends, exchange flows, regulatory signals),
  technical (RSI/SMA/BBANDS), quant (your bootstrap model + cross-name dispersion).

  Your assigned universe is in agents/sector_map.yaml. Bearish theses route
  via inverse ETFs from agents/inverse_etf_map.yaml — see [DESK POLICY: NO
  DIRECT SHORTS] in your injected system context.

  Hard rules:
  - Conviction is a positive number. Direction encodes the side.
  - Cash is a position too — vote symbol='CASH' direction='long' when you'd
    rather hold cash than your top long.
  - Refresh every hour: stale views auto-expire after 4 hours.
```

**Required fields**: `name`, `system_prompt`, `allocation_pct`. Optional:
`enabled` (default true), `risk_overrides`, `description`, model paths.

---

## 3. Create the two skill files

Copy from a structurally-similar existing agent (atlas is the canonical
template) and substitute the agent name + sector description. Both files live
in `.claude/commands/`.

### `.claude/commands/<name>-review.md` (hourly)

Adapted from `atlas-review.md`. Keep the 7-step flow:

0. Skip-fast guards (market_status / quiet_window / kill_switch)
1. Load full state — including the desk-wide threads board
2. Sector scan — quotes, bars, technicals, news, custom-indicator
3. **ULTRATHINK** per symbol in 3 frameworks (fundamental / technical / quant)
4. Publish — `clear_my_views(<name>)` then `submit_conviction_view` for each call
5. Journal continuity — grade due predictions, raise any tool gaps
6. Stdout summary block
7. Telegram analysis ping (≤350 chars, the canonical desk-wide voice)

The **STEP 1 desk-board paragraph** and the **STEP 3 cash-conviction
paragraph** are mandatory. Easiest method: copy `atlas-review.md` verbatim,
then sed/replace the agent name and the sector description in the header.

### `.claude/commands/<name>-evening.md` (end-of-day)

Adapted from `atlas-evening.md`. Loads the day's fills + the agent's
attribution, grades convictions vs. close, writes a one-row evening digest
via `record_evening_digest`. Future (post-2026-04-27 wave): also
`post_to_thread(thread_slug='<name>-reports', author='<name>', body=...)` so
the daily report shows up on the threads board.

---

## 4. Assign an IBKR client ID

Edit `scripts/run_scheduled_skill.sh`. Find the `case "$SKILL"` block; add
two entries (review + evening) using the next free integer. Example:

```bash
zara-review)     export IBKR_CLIENT_ID=37 ;;
zara-evening)    export IBKR_CLIENT_ID=38 ;;
```

**Rule**: each (skill name) → unique ID. IDs collide → IBKR rejects the
second connection with `Error 326: Unable to connect as the client id is
already in use`. Used IDs as of 2026-04-27: 11–36. Next free: 37.

Keep the matching `.bat` file (Windows launcher) in sync if it's still in
the repo.

---

## 5. Add to the orchestrator

Edit `scripts/run_hourly_orchestrator.sh`. Append the agent name to the
`SECTORS=(…)` array. The orchestrator fans this out via `xargs -P 4`
during phase 1, so the new agent's review runs every hour automatically.

```bash
SECTORS=(atlas commodity energy fab fabless iron maya rex trump vera volt zara)
```

If you have an evening review and it should run on the existing evening
schedule, add it to whatever timer file kicks off `*-evening` skills (or
add a new systemd timer in `scripts/systemd/` if there isn't one yet).

---

## 6. (Optional) Quant model

If the review prompt references `compute_custom_indicator(model='breakout_strength', symbol=…)`, drop a working model at:

```
agents/<name>/models/breakout_strength.py
```

Module must expose `compute(symbol, bars, context) -> dict`. Use
`agents/atlas/models/breakout_strength.py` as the template. Without this
file the review still works — `compute_custom_indicator` returns
`{"error": "model not found"}` and the agent proceeds with technical +
fundamental frameworks only.

---

## 7. Validate before letting it loose

Run all four checks. **Do not skip.**

1. **YAML parses + new agent visible**:
   ```bash
   .venv/bin/python -c "
   from agent.agent_registry import list_agents
   names = [a['name'] for a in list_agents(enabled_only=False)]
   assert 'zara' in names, names
   print('agent_registry OK:', names)"
   ```

2. **Sector map has no boolean keys** (the `ON` → `True` trap):
   ```bash
   .venv/bin/python -c "
   import yaml
   m = yaml.safe_load(open('agents/sector_map.yaml'))
   bad = [(a, k) for a, spec in m['agents'].items() for k in (spec.get('universe') or {}) if not isinstance(k, str)]
   assert not bad, bad
   print('sector_map OK')"
   ```

3. **IBKR client ID free** (gateway must be up):
   ```bash
   IBKR_CLIENT_ID=37 .venv/bin/python scripts/ibkr_preflight.py
   ```

4. **Dry-run the new review**:
   ```bash
   bash scripts/run_scheduled_skill.sh <name>-review
   tail -50 logs/<name>-review.log
   ```
   Expected: connects to IBKR, fetches quotes, calls
   `submit_conviction_view` ≥ once, exits 0. If it errors on
   `submit_conviction_view: '<SYM>' not in <name>'s sector universe`, your
   agent's prompt and `sector_map.yaml` universe disagree — fix the
   prompt or add the symbol.

After step 4 passes, the agent will participate in the next hourly
orchestrator run automatically. Watch the first run's mike-allocator output
to confirm the new agent's convictions made it into the consolidated stack
(`get_consolidated_view(caller='mike')`) and got a slice of NAV.

---

## 8. (Optional) Announce to the desk

Post to the threads board so peer agents and Mike know about the new agent:

```python
post_to_thread(
  thread_slug='desk-announcements',
  author='user',
  title=f'New agent <name> activated',
  body=f'<name> covers <sector>. Universe overlap with existing agents: <X>. '
       'Live as of <date>. Allocation_pct=0.0 (sector-conviction model).',
  expires_in_hours=72,
)
```

Optional: `create_thread(slug='<name>-reports', title='<Name>'s daily reports', tags=['reports','<name>'])` for the agent's evening digests to land in.

---

## Common gotchas

- **YAML boolean tickers** — `ON`, `OFF`, `YES`, `NO`, `Y`, `N`, `TRUE`,
  `FALSE` are all coerced to bools by PyYAML. Always quote them: `"ON":`.
  Symptom: `'bool' object has no attribute 'upper'` from
  `submit_conviction_view` or `rebalance_desk`.
- **Symbol not in universe.** `submit_conviction_view` validates membership
  via `_agent_owns_symbol` (see `mcp_server.py`). If the agent's prompt
  lists symbols that aren't in `sector_map.yaml`, every submission fails.
- **Universe collision.** Two agents covering the same name is legal — the
  allocator sums their signed convictions. But it dilutes attribution. If
  the new agent is genuinely a peer (e.g., crypto vs. macro), keep the
  overlap small.
- **Inverse ETF references.** Only verified entries in
  `agents/inverse_etf_map.yaml` will pass validation. Adding an inverse
  here requires audit (see the file's header comment).
- **Telegram chattiness.** Each new sector agent adds ~1 Telegram
  message/hour during open. With 10 sector agents already pinging, watch
  total volume: 11+ pings/hour × 6.5h = 70+ messages/day. Tune in the
  agent prompt if too noisy.
- **Mike's `_SECTION_KEYS`.** `agent/prompt_builder.py` has a hardcoded
  map of which agents get a personalized section in Mike's morning
  analysis (`<agent>_guidance`). New agents won't get one until you (a)
  add the agent to `_SECTION_KEYS` AND (b) update the mike-morning skill
  to write that section. Without it, the agent reads only the generic
  regime + risk_tone — works fine, just less guidance.

---

## Quick checklist (printable)

- [ ] Picked unique lowercase name; described sector clearly
- [ ] Defined 10–35-symbol universe with `bearish_via` per name
- [ ] Updated `agents/sector_map.yaml` (quoted any boolean-trap tickers)
- [ ] Created `agents/<name>.yaml` with `allocation_pct: 0.0`
- [ ] Created `.claude/commands/<name>-review.md` (with STEP 1 board + STEP 3 cash blocks)
- [ ] Created `.claude/commands/<name>-evening.md`
- [ ] Assigned IBKR client ID in `run_scheduled_skill.sh` (review + evening)
- [ ] Added to `SECTORS=(…)` in `run_hourly_orchestrator.sh`
- [ ] (Optional) Quant model at `agents/<name>/models/breakout_strength.py`
- [ ] All 4 validations pass
- [ ] Smoke-test review run exits 0 with ≥1 `submit_conviction_view` call
- [ ] Announced on `desk-announcements` thread
