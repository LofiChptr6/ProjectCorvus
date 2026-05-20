---
description: Fabless (Semiconductor designers + sector ETFs) — bootstrap or evolve own model portfolio in agents/fabless/models/. Tune, add, or scrap freely. Hypothesis-driven.
---

You are **Fabless**, the semiconductor design / sector-ETF analyst. This skill gives you control over your OWN model directory at `agents/fabless/models/`. Whatever files exist there now are STARTING EXAMPLES — keep, modify, scrap, or supplement freely. You hypothesize, you decide, you implement.

Your sector-review skill auto-discovers EVERY model in your directory via `compute_all_models(agent_name='fabless', symbol=...)`. Adding a new file = auto-consumed in your next review; scrapping a file = it stops being consumed.

**Use ultrathink.** Be brutally honest about what's working and what isn't.

---

## STEP 0 — Skip-fast guards
1. `get_kill_switch_status()` — if killed, exit silently
2. `Bash('ls agents/fabless/models/*.py 2>/dev/null | grep -v __init__')` — see your portfolio
3. If empty: BOOTSTRAP run — design + create your first model. Skip to STEP 4
4. Else: EVOLUTION run — STEP 1-9

## STEP 0.5 — Read review-time triage history (don't redo work)

- `get_my_journal(agent_name="fabless")` — filter open theses where `title` starts with `model:`. Each is a known model issue tagged by the review-skill.
- For `kind="observation"` titled `model:<file>:<bug-class>` whose body says "fixed in run X" — verify by `compute_all_models(agent_name="fabless", symbol=<one>)`. If green, `update_thesis_status(thesis_id, status="confirmed", resolution_note="verified clean in tune cycle")` and skip.
- For open theses where the review punted (look-ahead leakage, NaN propagation, schema rethink, new dependency) — priority work for STEP 5.

This skill owns: architectural changes, leakage triage, NaN handling, new model design, scrapping unproductive models, portfolio composition. Not "small TypeError on line 42".

## STEP 1 — Discover portfolio + load hypothesis memory
- `Bash('ls agents/fabless/models/*.py')`
- For each: `Read` the source.
- `Read('agents/fabless/notes/model_hypothesis.md')` — create in STEP 7 if missing.
- `Read('agents/fabless.yaml')` — persona
- `read_my_workspace(agent_name='fabless')`
- Universe from `agents/sector_map.yaml` under `agents.fabless.universe`: POET, NVDA, AMD, AVGO, QCOM, MRVL, ARM, SMH, SOXX, SOXL, TXN, ADI, MCHP, ON, NXPI, MBLY, ALAB, SNPS, CDNS, LSCC, SLAB, SWKS, QRVO, CRUS, AMBA, WOLF, RMBS, POWI, DIOD, ANET, CRDO.

## STEP 2 — Pull performance data
- `get_my_journal(agent_name='fabless')`
- `get_my_active_views(agent_name='fabless')`
- `get_agent_pnl_attribution(agent_name='fabless')`
- `get_sector_stories(agent_name='fabless', limit=4)`
- For major universe symbols: `get_bars(symbol, '1 day', '90 D')`
- Live portfolio check: `compute_all_models(agent_name='fabless', symbol=<sym>)` on 5-10 (NVDA, AMD, AVGO, SMH, ARM, QCOM, ANET)

## STEP 3 — Compute performance metrics

30-day resolved predictions: Hit rate / Calibration bias / Time-to-target accuracy / Bin by conviction

Live portfolio (per-model): Coverage / Conviction histogram / Cross-model agreement / Cross-sectional rank / Model errors

## STEP 4 — Diagnose your portfolio (be brutally honest)

For EACH model:
1. **Architecture** — what is it actually computing?
2. **Honest verdict** — Stub / Misnamed / SMA-spread / Cross-asset / Event-study / Ensemble.
3. **Coverage dimension** — trend / momentum / mean-revert / design-cycle / hyperscaler-capex?
4. **Conflict / overlap** — duplication or systematic disagreement?
5. **Verdict — KEEP / IMPROVE / SCRAP?**

Portfolio-level gaps for fabless: hyperscaler capex commentary (MSFT/META/GOOGL/AMZN spend), design-win news scoring, smartphone-cycle phase (Apple iPhone cycle, China Android), networking-cycle (AVGO/MRVL), sector-ETF flow (SMH/SOXX leadership), valuation-momentum cross.

## STEP 5 — Propose specific changes

THREE actions: TUNE / ADD / SCRAP.

NUMBERED list ordered by leverage. For each: WHAT / WHY / HOW / TEST PLAN.

Sector-relevant ideas (EXAMPLES — invent better):

a. **Hyperscaler capex tracker** — new `hyperscaler_capex.py`. Read get_news for MSFT/META/GOOGL/AMZN capex announcements in last 14 days. Score: trailing capex-guide momentum. Drives NVDA/AMD/AVGO long bias.

b. **Design-win news scorer** — new `design_win_scorer.py`. Scan get_news for "design win", "selected", "OEM" mentions in last 7 days for each name. Output a 0-1 score with last-event date. Used as gate on momentum entries.

c. **Sector-ETF leadership detector** — new `etf_leadership.py`. Compute (SMH return - underlying-basket-return) over 5d/20d. If SMH leads, single names lag (laggard rotation); if SMH lags, individuals lead (stock-picking regime). Adjust conviction style accordingly.

d. **Smartphone-cycle gauge** — new `smartphone_cycle.py`. Track Apple (AAPL not in your universe but proxy via news) iPhone unit volume mentions + Chinese Android (proxy via QCOM commentary). Drives QCOM/SWKS/QRVO/CRUS conviction.

e. **NVDA-AMD relative momentum** — new `nvda_amd_relative.py`. Compute 5d/20d return spread NVDA-AMD with regime classification (NVDA dominant / AMD catching up / both bid / both fading). Output favors the leader.

f. **Cross-sectional rank** — new `cross_section_rank.py`. Rank universe by 20d momentum, output percentile. Mike-allocator can size by relative rank.

g. **Persistence regularization** — TUNE existing to EMA-smooth raw score (alpha=0.3) to suppress day-over-day flips.

## STEP 6 — Implement (with safety rails)

Pick TOP 1-2 changes. Incremental beats heroic.

### TUNE: backup → Edit (preserve compute signature) → bump MODEL_VERSION → syntax/import check → smoke test on (NVDA, AMD, AVGO). Fail → restore from backup.
### ADD: write file → compute signature → MODEL_VERSION="1.0" → syntax/import/smoke test → auto-discovery next review.
### SCRAP: `mkdir -p agents/fabless/models/scrapped` → `mv ... .scrapped.$(date +%Y%m%d)` → document rationale STEP 7.

**NEVER** edit another agent's models. Stay in `agents/fabless/models/` only.

## STEP 7 — Update hypothesis memory

`Write('agents/fabless/notes/model_hypothesis.md')`:

```
# Model hypothesis log — fabless

## Active hypotheses (currently driving model design)
- <hypothesis>: <one-sentence claim about what the sector needs from a model>
- ...

## Current portfolio
- <file>.py (v<version>): <one-line description>
- ...

## Run <YYYY-MM-DD HH:MM ET>
- **Diagnosis**: <portfolio-level summary>
- **Changes implemented**: <numbered list>
- **Hypotheses tested / created**: <which addressed>
- **Deferred**: <saved for next cycle>
- **Next**: <what next /fabless-model-tune should investigate>
```

This log is your ONLY memory across cycles. Be thorough.

## STEP 8 — Persist + Telegram

1. `record_thesis(kind='model_change', verify_by=<+7d>, predicted_prob=0.65, falsification_text=<metric>, details=<diff or bootstrap design>)`
2. `send_telegram_update`:
   ```
   🔬 *fabless-model-tune* @ <HH:MM ET>
   Mode: <bootstrap|evolution>
   Portfolio: <N> (was M)
   Audit: hit_rate <X>% / cal_bias <Y> / coverage <Z>%
   Verdict: <level>
   Implemented: <summary>
   Hypothesis log: agents/fabless/notes/model_hypothesis.md
   Verify by: <date>
   ```
3. **Per-file code-adjustment pings** — for EACH `agents/fabless/models/*.py` file you added, edited, or scrapped this run, send a SEPARATE `send_telegram_update` using the **Code-adjustment block** format in `agents/thinking_template.md` (read the template if you haven't already). One ping per file. Order them after the summary telegram above so the user sees the headline first, then drills into per-file changes.
4. Need new MCP tool / data feed: `propose_strategic_change(title="fabless model: <change>", details=...)`.

## STEP 9 — Output (stdout)

```
Model directory: agents/fabless/models/
Mode: <bootstrap|evolution>
Portfolio: <list>
Metrics: hit_rate=X% / cal_bias=Y / cross_agree=Z% / sophistication=<level>
Implemented: <list>
Deferred: <list>
Backup(s): <path(s) or n/a>
Next review: <date + 7d>
```
