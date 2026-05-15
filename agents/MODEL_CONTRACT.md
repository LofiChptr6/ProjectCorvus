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
  failure — one missing extra shouldn't kill the conviction for everything else.
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

## `compute()` output

A single dict. All numeric fields must come from a real computation on `bars` —
never from `context` echoing, never hardcoded "plausible" defaults.

| Key | Type | Meaning |
|---|---|---|
| `signal` | `float \| None` | One-number summary of the setup (-1..+1 conventionally). **`None` means "model declines to publish"** — runner skips conviction write. |
| `direction` | `"long" \| "short" \| "flat" \| None` | Which way the model thinks. `"flat"` = explicit neutral; `None` = no decision. Both cause the runner to skip. |
| `conviction` | `float ∈ [0, 1]` | Model's confidence. Drives Mike's allocation weight. Don't let `1.0` mean anything other than "I'd put the whole desk on this". |
| `expected_return_pct` | `float` | Magnitude of the move expected between now and `time_to_target_days`. **Signed**, matches `direction`. |
| `time_to_target_days` | `int` | When this prediction should be evaluated. The resolver uses this. |
| `stop_pct` | `float \| None` | Adverse move from entry that invalidates. Risk-derived (e.g. 1× ATR, recent swing low) — not a guess. |
| `inputs` | `dict[str, float]` | Features the model actually used. The **replay payload** — must be recomputable from `bars` alone. Used by `model_inputs_validator` to detect fabrication. |
| `rationale` | `str` (optional) | One-sentence human gloss. Fine to omit. |
| `interpretation` | `str` (optional) | Short label ("strong breakout", "weak setup"). Fine to omit. |

## The no-signal return

When the model declines (insufficient bars, gating filter failed, neutral
state), return this shape verbatim:

```python
return {
    "signal": None,
    "direction": None,
    "conviction": 0.0,
    "expected_return_pct": 0.0,
    "time_to_target_days": 0,
    "inputs": {},
    "reason": "need >=22 bars, got 18",   # human-readable, for logs
}
```

The runner uses `signal is None or direction in (None, "flat")` as the gate to
skip writing.

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
7. **`conviction = 0.0` for no signal.** Never emit `conviction > 0` alongside
   `direction=None` or `signal=None`.

## What goes in `inputs`?

Only features the model **literally used** in its decision. If the model
computed RSI but didn't branch on it, RSI doesn't go in `inputs`. The point of
the replay payload is "given these numbers, the model output is determined".

The validator (`meta_agent/model_inputs_validator.py`) introspects every
model's `inputs` keys at startup. Convictions that arrive with `model_inputs`
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
        return {"signal": None, "direction": None, "conviction": 0.0,
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
            "conviction": min((-z - 2.0) / 2.0, 1.0),
            "expected_return_pct": 2.5,
            "time_to_target_days": 5,
            "stop_pct": -3.0,
            "inputs": {"z5": round(z, 3), "above_sma200": 1.0},
            "interpretation": "oversold above trend",
        }
    return {"signal": None, "direction": None, "conviction": 0.0,
            "expected_return_pct": 0.0, "time_to_target_days": 0,
            "inputs": {}, "reason": "no setup"}
```

## How models become convictions

`submit_conviction_from_model(agent_name, model_name, symbol, rationale)`
(server-side) is the **only** path that writes to `agent_conviction`:

1. Loads the model via `meta_agent.model_loader`.
2. Fetches `LOOKBACK_DAYS` of bars at `BAR_FREQUENCY` from Massive.
3. Builds `context` from the desk state at run time.
4. Calls `compute(symbol, bars, context)`.
5. If `signal is None` or `direction in (None, "flat")`: returns `{skipped: true, reason: ...}`.
6. Otherwise inserts the conviction with numbers taken directly from the
   model output. The agent supplies only the human-readable `rationale`
   string; everything else (`conviction`, `expected_return_pct`,
   `time_to_target_days`, `stop_pct`, `model_inputs`) comes from `compute()`.

The legacy `submit_conviction_view` tool (where the agent supplies the
numbers) is being retired. During the migration it accepts only convictions
whose `model_inputs` keys match the registry — see
`meta_agent/model_inputs_validator.py`.

## When a model needs to evolve

- Behavior change → bump `MODEL_VERSION`.
- New feature in `inputs` → add it, then run the validator's registry rebuild.
- Retired model → move to `agents/<name>/models/scrapped/`; do not delete.
  The forecast/conviction history references the file path indirectly via
  `method` and `model_inputs`, so the audit trail breaks if files vanish.

## Why this contract exists

Before this contract was enforced, LLM agents fabricated thesis price anchors
(rex stamped `AAPL=198.45` across 14 hourly theses with real last 298),
indicator readings (RSI/BBAND keys for agents whose real models emit
`z`/`above_sma200`), and conviction numbers that bore no relation to the
agent's own models. The price-anchored resolver was about to grade fake
anchors against real closes — a tautology.

This contract makes hallucination impossible at the field level: prediction
numbers come from Python; the LLM owns model selection, model authorship, and
the rationale prose.
