# Quant Model Contract

Every model under `agents/<name>/models/*.py` is the **only** path through which
a numeric prediction lands in `agent_conviction` / `agent_forecast`. The agent
LLM picks which model to run on which symbol and writes the rationale text.
The numbers come from the model.

This file is the contract those models must satisfy. New models that don't
conform won't load.

## File layout

```
agents/<agent_name>/models/<model_name>.py
agents/<agent_name>/models/scrapped/   # retire here instead of deleting
agents/<agent_name>/models/__init__.py # empty marker
```

`agent_name` and `model_name` must match `[a-z][a-z0-9_]{0,31}`. Subdirectories
(other than `scrapped/`) are ignored by the loader.

## Required surface

```python
MODEL_VERSION  = "1.0"        # bump on any compute() behavior change
BAR_FREQUENCY  = "1d"         # one of: "1m", "5m", "15m", "1h", "1d"
MIN_BARS       = 22           # minimum bars required to emit a signal
LOOKBACK_DAYS  = 60           # bars the runner will fetch (default 252)
EXTRA_SYMBOLS  = []           # optional: other tickers whose bars the runner
                              # should also fetch and pass under
                              # context["extra_bars"]. Default [].

def compute(symbol: str, bars: list[dict], context: dict) -> dict:
    ...
```

## Extra symbols (cross-asset signals)

A model can declare `EXTRA_SYMBOLS = ["VIX", "XLV", ...]` to ask the runner to
fetch bars for additional tickers at the same `BAR_FREQUENCY` and
`LOOKBACK_DAYS` as the main symbol. They arrive in `context["extra_bars"]`:

```python
context["extra_bars"] = {
    "VIX": [{o, h, l, c, v}, ...],
    "XLV": [{o, h, l, c, v}, ...],
}
```

Semantics:
- **Per-symbol fetch failure → empty list.** The model is expected to check
  `len(context["extra_bars"].get(sym, [])) >= N` and return the no-signal shape
  if it can't compute. The runner does NOT short-circuit on extra-fetch
  failure — one missing extra shouldn't kill the prediction for everything else.
- **Same frequency as main bars.** No per-extra `BAR_FREQUENCY` override.
- **Validator behavior.** The startup registry build (`model_inputs_validator`)
  uses synthetic bars and re-uses them as synthetic stand-ins for every
  declared extra so the model's `inputs` keys still register. This means: when
  designing the model, make sure the compute path that emits the full `inputs`
  dict is reachable under synthetic OHLCV (random ±2% bars, prices around 100).
  In practice this means including the inputs dict on the no-setup branch too.

Compute the cross-asset feature INSIDE the model — don't expect pre-derived
scalars in `context`. This keeps the helper sector-agnostic and lets each model
pick its own definition of "RSI" / "curve slope" / "yield-change proxy".

## `compute()` inputs

| Arg | Shape | Notes |
|---|---|---|
| `symbol` | `str` | Ticker. Already uppercased. |
| `bars` | `list[dict]` | OHLCV bars, oldest first. Each bar `{o, h, l, c, v}` floats/ints. Length = `LOOKBACK_DAYS` worth at `BAR_FREQUENCY`. |
| `context` | `dict` | Sector regime, macro flags, `as_of` timestamp, etc. Always optional — never `KeyError` on a missing key. |

## `compute()` output — the forecast triple

A single dict. All numeric fields must come from a real computation on `bars` —
never from `context` echoing, never hardcoded "plausible" defaults.

| Key | Type | Meaning |
|---|---|---|
| `signal` | `float \| None` | One-number summary of the setup (-1..+1 conventionally). **`None` means "model declines to publish"** — runner skips the write. |
| `direction` | `"long" \| "short" \| "flat" \| None` | Which way the model thinks. `"flat"` = explicit neutral; `None` = no decision. Both cause the runner to skip. |
| `expected_return_pct` | `float` | Magnitude of the move expected between now and `time_to_target_days`. **Signed**, matches `direction`. |
| `likelihood` | `float ∈ [0, 1]` | Model's calibrated probability that the forecast plays out. 0 → no edge; 1 → full-confidence call. **This is the only confidence number a model emits.** The desk computes its own internal allocator weight as `\|expected_return_pct\| × likelihood / time_to_target_days` — models do NOT author that weight. |
| `time_to_target_days` | `int` | When this prediction should be evaluated. The resolver uses this. |
| `stop_pct` | `float \| None` | Adverse move from entry that invalidates. Risk-derived (e.g. 1× ATR, recent swing low) — not a guess. |
| `inputs` | `dict[str, float]` | Features the model actually used. The **replay payload** — must be recomputable from `bars` alone. Used by `model_inputs_validator` to detect fabrication. |
| `rationale` | `str` (optional) | One-sentence human gloss. Fine to omit. |
| `interpretation` | `str` (optional) | Short label ("strong breakout", "weak setup"). Fine to omit. |
| `distributions` | `list[dict]` (optional) | Probabilistic per-horizon forecasts; see "Probabilistic distributions" below. When present, persisted to `agent_forecast` with a fresh `forecast_run_id` and the registered functional collapses them into a scalar `likelihood` (overrides any `likelihood` you set). |

### Legacy `conviction` field (transitional shim)

Older models emitted a field named `conviction ∈ [0, 1]` that served the same
role this contract now assigns to `likelihood`. The runner accepts either name:
if `likelihood` is missing it falls back to the value under `conviction`.
**New models must use `likelihood`.** When migrating an existing model, rename
the dict key — leave nothing else in the code under the old name.

The runner does not persist the model's emitted `conviction`/`likelihood` value
as the desk's allocator weight. That weight is recomputed centrally from
the triple `(expected_return_pct, likelihood, time_to_target_days)`. There is
no path through which the LLM agent or a quant model can pick the allocator
weight directly.

## Probabilistic distributions

The optional `distributions` field is the **richer, replacing** belief format —
a list of discrete probability vectors per horizon. When a model emits
distributions:

1. The runner validates each entry against `meta_agent/distribution_validator.py`.
2. A fresh `forecast_run_id` (UUID) is allocated and stamped on every
   resulting row in `agent_forecast` plus the scalar in `agent_conviction`.
3. The registered functional (`meta_agent/conviction_functionals/`) collapses
   the per-horizon distributions into a single scalar `likelihood ∈ [0, 1]`,
   which overrides the model's own `likelihood` field (the model can omit it).
   The desk then computes the allocator weight from that scalar likelihood
   plus the model's expected_return_pct and time_to_target_days as usual.

Each distribution entry:

```python
{
    "anchor_price":  195.50,           # current price the bins are anchored to
    "anchor_ts":     "2026-05-16T17:30:00+00:00",
    "axis":          "return_pct",     # "return_pct" | "log_return"
    "horizon":       "5m",             # "5m" | "1h" | "1d" | "1w"
    "bins": [
        {"x": -2.0, "p": 0.05},        # x in axis units (here, percent return)
        {"x": -1.0, "p": 0.20},
        {"x":  0.0, "p": 0.50},
        {"x":  1.0, "p": 0.20},
        {"x":  2.0, "p": 0.05},
    ],
    "model":         "ou_mean_revert",
    "model_version": "0.1.0",
}
```

Validation rules (enforced at submit):
- bins length in `[3, 20]`
- x strictly increasing AND uniformly spaced (required for CRPS calibration)
- each `p ≥ 1e-4` (additive smoothing — prevents `-inf` log-loss if realized
  return lands in a zero-mass bin)
- `sum(p)` within `1e-4` of 1.0
- horizon ∈ `{5m, 1h, 1d, 1w}`; axis ∈ `{return_pct, log_return}`

A model emitting `distributions` should still populate `direction` and
`expected_return_pct` (mirror E[r] across the longest horizon) for back-compat
consumers, but `likelihood` is recomputed from the distributions.

## The no-signal return

When the model declines (insufficient bars, gating filter failed, neutral
state), return this shape verbatim:

```python
return {
    "signal": None,
    "direction": None,
    "likelihood": 0.0,
    "expected_return_pct": 0.0,
    "time_to_target_days": 0,
    "inputs": {},
    "reason": "need >=22 bars, got 18",   # human-readable, for logs
}
```

The runner uses `signal is None or direction in (None, "flat")` as the gate to
skip the write.

## Rules

1. **No live I/O.** The model must not call Massive, IBKR, news, or the DB.
   Bars and context are the only world the model sees. Keeps backtests
   deterministic and makes the validator's synthetic-bars introspection work.
2. **No wall-clock.** Use `context.get("as_of")` if you need a timestamp.
3. **Deterministic given (symbol, bars, context).** Reruns must produce
   identical output. No `random` without a seeded RNG; no time-of-day
   branching.
4. **Fast.** Target <50ms per symbol. The runner fans models across the whole
   universe every hourly tick.
5. **`inputs` keys are stable.** The validator pins each agent to the set of
   `inputs` keys observed across all of its models. Renaming a key without
   bumping `MODEL_VERSION` and updating callers will break the validator.
6. **Sign discipline.** If `direction="short"`, `expected_return_pct` must be
   negative. If `"long"`, positive. The allocator trusts this.
7. **`likelihood = 0.0` for no signal.** Never emit `likelihood > 0` alongside
   `direction=None` or `signal=None`.

## What goes in `inputs`?

Only features the model **literally used** in its decision. If the model
computed RSI but didn't branch on it, RSI doesn't go in `inputs`. The point of
the replay payload is "given these numbers, the model output is determined".

The validator (`meta_agent/model_inputs_validator.py`) introspects every
model's `inputs` keys at startup. Submissions that arrive with `model_inputs`
keys not in any of an agent's models' output will be rejected.

## Example: minimal compliant model

```python
"""Mean reversion on 5-day z-score. If z < -2 and price > 200-DMA, long."""
from __future__ import annotations
from statistics import mean, pstdev

MODEL_VERSION  = "1.0"
BAR_FREQUENCY  = "1d"
MIN_BARS       = 200
LOOKBACK_DAYS  = 210

def compute(symbol, bars, context):
    if len(bars) < MIN_BARS:
        return {"signal": None, "direction": None, "likelihood": 0.0,
                "expected_return_pct": 0.0, "time_to_target_days": 0,
                "inputs": {}, "reason": f"need >={MIN_BARS} bars"}

    closes = [b["c"] for b in bars]
    last = closes[-1]
    sma200 = mean(closes[-200:])
    recent = closes[-5:]
    z = (last - mean(recent)) / (pstdev(recent) or 1e-9)

    if z < -2.0 and last > sma200:
        return {
            "signal": -z,
            "direction": "long",
            "likelihood": min((-z - 2.0) / 2.0, 1.0),
            "expected_return_pct": 2.5,
            "time_to_target_days": 5,
            "stop_pct": -3.0,
            "inputs": {"z5": round(z, 3), "above_sma200": 1.0},
            "interpretation": "oversold above trend",
        }
    return {"signal": None, "direction": None, "likelihood": 0.0,
            "expected_return_pct": 0.0, "time_to_target_days": 0,
            "inputs": {}, "reason": "no setup"}
```

## How models become rows in `agent_conviction`

`submit_conviction_from_model(agent_name, model_name, symbol, rationale)`
(server-side) is the **only** path that writes to `agent_conviction`:

1. Loads the model via `meta_agent.model_loader`.
2. Fetches `LOOKBACK_DAYS` of bars at `BAR_FREQUENCY` from Massive.
3. Builds `context` from the desk state at run time.
4. Calls `compute(symbol, bars, context)`.
5. If `signal is None` or `direction in (None, "flat")`: returns `{skipped: true, reason: ...}`.
6. Otherwise inserts the row. `expected_return_pct`, `likelihood`,
   `time_to_target_days`, `stop_pct`, `model_inputs` are taken directly from
   the model output. The desk-internal allocator weight is recomputed
   centrally via `meta_agent.allocator.compute_conviction` — neither the
   model nor the agent picks it. The agent supplies only the human-readable
   `rationale` string.

The legacy `submit_conviction_view` tool (where the agent supplies the
numbers) accepts only the forecast triple `(expected_return_pct, likelihood,
time_to_target_days)`. It still rejects submissions whose `model_inputs`
keys don't match the registry — see `meta_agent/model_inputs_validator.py`.

## When a model needs to evolve

- Behavior change → bump `MODEL_VERSION`.
- New feature in `inputs` → add it, then run the validator's registry rebuild.
- Renaming legacy `conviction` → `likelihood` → minor MODEL_VERSION bump;
  the runner accepts both during the migration window but emit a clean
  `likelihood` going forward.
- Retired model → move to `agents/<name>/models/scrapped/`; do not delete.
  The forecast history references the file path indirectly via `method`
  and `model_inputs`, so the audit trail breaks if files vanish.

## Why this contract exists

Before this contract was enforced, LLM agents fabricated thesis price anchors
(rex stamped `AAPL=198.45` across 14 hourly theses with real last 298),
indicator readings (RSI/BBAND keys for agents whose real models emit
`z`/`above_sma200`), and self-tuned conviction numbers that bore no relation
to the agent's own models. The price-anchored resolver was about to grade fake
anchors against real closes — a tautology.

This contract makes hallucination impossible at the field level: prediction
numbers come from Python; the LLM owns model selection, model authorship, and
the rationale prose. The desk's internal allocator weight is computed once,
centrally, from the same `(expected_return_pct, likelihood, time_to_target_days)`
triple that the resolver later grades — closing the loop between forecast
authorship, sizing, and accountability.
