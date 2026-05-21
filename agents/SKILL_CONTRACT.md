# Skill Contract

Companion to `MODEL_CONTRACT.md` and `CITATION_ARCH.md` Phase B.

A **skill** is a lightweight, agent-callable Python function that answers
ONE specific question with an evidence_id. Unlike models (heavy quant
computations that produce a forecast triple + distribution and write to
`agent_forecast`), skills return a single result the LLM can pin to a
Citation.

Skills live under `agents/<name>/skills/<skill_name>.py`. Common skills are
authored once in `agents/atlas/skills/` and symlinked into each sector
agent's `skills/` directory — matching the pattern for atlas's
distribution-emitting models.

---

## File layout

```
agents/<agent>/skills/<skill_name>.py
agents/<agent>/skills/__init__.py        # empty marker
```

`agent` and `skill_name` must match `[a-z][a-z0-9_]{0,31}`. Subdirectories
are ignored by the loader.

---

## Required surface

```python
"""Docstring — first line is the description shown in the bundle if
SKILL_DESCRIPTION isn't set."""

SKILL_VERSION = "0.1.0"
SKILL_DESCRIPTION = "One-line description for the review prompt."

async def compute(
    *,
    agent_name: Optional[str] = None,
    session_id: Optional[str] = None,
    **kwargs,
) -> dict:
    """Run the skill. Returns {ok, result, inputs_used, evidence_id, ...}
    on success, or {ok: False, reason} on graceful decline.
    """
```

Notes:
- `compute` MUST be `async` — even if the work is sync, the loader awaits it.
- `agent_name` and `session_id` are reserved kwargs the loader injects.
  Skills MUST accept them (use `**_unused` to swallow if not needed).
- Skill-specific args (e.g. `symbol`, `terms`, `asof`) are positional-friendly
  keyword args; the loader passes them via `**args`.

---

## Return shape

### Success

```python
{
    "ok": True,
    "result": <any>,        # the answer the LLM cites
    "inputs_used": dict,    # literal args that produced result (for replay)
    "evidence_id": int,     # REQUIRED — points at an evidence_snapshot row
    # ...extra fields the skill wants to surface (e.g. last_close, confidence)
}
```

### Graceful decline

```python
{"ok": False, "reason": "<short explanation>"}
```

Graceful declines are normal: insufficient bars, symbol not in cache,
empty news window, etc. The runner surfaces `reason` to the LLM so it can
adapt its plan.

---

## Evidence stamping (the load-bearing contract)

A skill returning `ok=True` **MUST** include an `evidence_id` pointing at
a row in `evidence_snapshot`. The skill is responsible for inserting that
row — typically by calling another tool that already stamps (e.g.
`tools.analysis.compute_indicator.execute`) and forwarding its
`evidence_id`, or by calling `db.store.stamp_evidence` directly.

The loader does NOT stamp on the skill's behalf because the skill knows
its semantic content_hash. Stamping at the wrong granularity would defeat
deduplication.

Pattern for skills that wrap a single underlying tool:

```python
async def compute(symbol: str, *, agent_name=None, session_id=None, **_):
    from tools.analysis.compute_indicator import execute
    res = await execute(symbol=symbol, indicator="RSI_14",
                        agent_name=agent_name, session_id=session_id)
    if not res["ok"]:
        return {"ok": False, "reason": res["reason"]}
    return {
        "ok": True,
        "result": res["value"],
        "inputs_used": {"symbol": symbol, "asof": res["asof"]},
        "evidence_id": res["evidence_id"],
    }
```

Pattern for skills that compose multiple tools or compute something not
covered by an existing tool:

```python
from db import store

async def compute(...):
    # ... compute the answer locally ...
    evidence_id = await store.stamp_evidence(
        kind="computed_indicator",   # one of the 5 Citation kinds
        source_ref_id=f"{symbol}:my_skill:{asof}",
        outputs_json={"value": value, ...},
        inputs_json={"symbol": symbol, ...},
        computed_by="my_skill@0.1.0",
        agent_name=agent_name,
        session_id=session_id,
    )
    return {"ok": True, "result": value, "inputs_used": {...},
            "evidence_id": evidence_id}
```

---

## Rules

1. **No live I/O outside the canonical sources.** Skills may call:
   - `tools.analysis.*` (compute_indicator, query_news, verify_catalyst)
   - `db.store.*` (reads only — no upserts; stamp_evidence is allowed)
   - Pure-Python computation on the returned data

   Skills MAY NOT call: IBKR, the Massive API directly, the news scraper.
   Going through `tools.analysis.*` keeps the evidence trail one-deep.

2. **Deterministic given the same inputs.** No `random` without a seeded
   RNG. No wall-clock; use `asof` if you need a timestamp.

3. **Fast.** Target <500ms per call. Skills are inline in the agent review
   loop; long-running computations belong as models in
   `agents/<name>/models/`, dispatched via the `agent_job` queue.

4. **One question per skill.** Composite skills (e.g. "is X above SMA200
   AND has news this week") are FINE, but the answer is still one
   structured result. Splitting into multiple skills hurts cite-ability.

5. **`evidence_id` is non-optional on success.** A skill that returns
   `ok=True` without `evidence_id` raises a contract violation in the
   loader (caller sees `status=error`).

6. **Authorship**: hourly reviews are registry-only. New skills are
   authored during the nightly `*-model-tune` channel (Phase E of
   CITATION_ARCH, currently deferred). Hand-authored skills are committed
   via normal PR review.

---

## Skill vs. model — when to use which

| Use a **skill** when | Use a **model** when |
|---|---|
| You want ONE answer to cite (RSI value, news count, ATR stop). | You want a full forecast triple (direction, ER, likelihood, ttd). |
| The answer is a scalar, bool, or small dict. | The output is a probability distribution. |
| You compose existing tools or do light math. | You fit a statistical model on bars. |
| Result feeds a Citation. | Result feeds `agent_conviction` via `submit_conviction_from_model`. |

Skills CAN be promoted to models when an agent finds they consistently
feed into the same kind of forecast. The promotion path runs through the
nightly model_tune session — see `CITATION_ARCH.md` §7 Phase B for the
flow.

---

## Example: minimal compliant skill

```python
"""Is this symbol's RSI_14 below 30 (oversold)?"""
from __future__ import annotations
from typing import Any, Optional

SKILL_VERSION     = "0.1.0"
SKILL_DESCRIPTION = "Is the symbol's 14-day RSI below 30? Returns bool with evidence_id."


async def compute(
    symbol: str,
    *,
    asof: Optional[str] = None,
    agent_name: Optional[str] = None,
    session_id: Optional[str] = None,
    **_unused: Any,
) -> dict[str, Any]:
    from tools.analysis.compute_indicator import execute
    res = await execute(symbol=symbol, indicator="RSI_14", asof=asof,
                        agent_name=agent_name, session_id=session_id)
    if not res.get("ok"):
        return {"ok": False, "reason": res.get("reason")}
    rsi = float(res["value"])
    return {
        "ok": True,
        "result": rsi < 30,
        "inputs_used": {"symbol": symbol.upper(), "asof": res["asof"], "rsi": rsi},
        "evidence_id": res["evidence_id"],
    }
```

---

## How a skill becomes a row in `agent_conviction` (via Citation)

```
LLM:  run_skill("energy", "compute_above_sma200", args={"symbol": "XOM"})
  ↓
Harness: skill returns {ok, result=True, evidence_id=42, ...}
  ↓
LLM:  Build ConvictionView with
        citations=[Citation(
          kind="computed_indicator",
          evidence_id=42,
          source_ref_id="XOM:ABOVE_SMA200:2026-05-21",
          quote="XOM trades above SMA_200 (last_close=155.29)",
        )]
  ↓
Runner: upserts the conviction; the citation evidence is durably linked
        and can be replayed by the verifier worker (Phase C).
```

The point: every load-bearing claim in the rationale traces to a
specific, replayable evidence row. The 2026-05-21 audit found this trail
missing on 100% of fabricated rationales; the contract above is how we
make it impossible to publish a non-traced numeric claim.
