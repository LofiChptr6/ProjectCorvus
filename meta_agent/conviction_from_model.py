"""Shared helper: turn a quant model into a conviction-ready payload.

Two call sites use this:
  - mcp_server.submit_conviction_from_model (one-shot tool)
  - pipelines.runner._apply_review_output  (batched per-review when a
    ConvictionView carries `from_model`)

The helper does NOT write to the DB, NOT check rate-limits, NOT check sector
ownership — callers handle those. It only handles model lifecycle: discover →
import → run → validate output against MODEL_CONTRACT.md.

See agents/MODEL_CONTRACT.md for the compute() output spec.
"""
from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


_BAR_FREQ_TO_SIZE: dict[str, str] = {
    "1m": "1 min", "5m": "5 mins", "15m": "15 mins",
    "30m": "30 mins", "1h": "1 hour", "1d": "1 day",
}


def _market_date() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date().isoformat()


async def compute_conviction_payload(
    agent_name: str,
    model_name: str,
    symbol: str,
    *,
    nav: Optional[float] = None,
    regime: Optional[str] = None,
) -> dict[str, Any]:
    """Load agents/<agent>/models/<model>.py, fetch bars at its declared
    BAR_FREQUENCY for LOOKBACK_DAYS, run compute(symbol, bars, context), and
    return one of:

      {"status": "ok", "payload": {direction, conviction, expected_return_pct,
                                   time_to_target_days, stop_pct, model_inputs},
       "model_version": "..."}
      {"status": "skipped", "reason": "...", "model_version": "..."}
      {"status": "error", "error": "..."}

    `nav` and `regime` populate the model's `context` dict — if not supplied, the
    function fetches them itself (best-effort).

    Side-effect-free except for the bar fetch + optional account_summary fetch.
    Both data-source failures degrade gracefully: bars failure → error;
    account_summary failure → empty context with no nav/regime.
    """
    from meta_agent.model_loader import discover_models

    if model_name not in discover_models(agent_name):
        return {"status": "error", "error": f"model not found: agents/{agent_name}/models/{model_name}.py"}

    try:
        module = importlib.import_module(f"agents.{agent_name}.models.{model_name}")
        module = importlib.reload(module)
    except Exception as exc:
        return {"status": "error", "error": f"model import failed: {type(exc).__name__}: {exc}"}

    if not hasattr(module, "compute"):
        return {"status": "error", "error": "model lacks compute(symbol, bars, context) entry point"}

    model_version = str(getattr(module, "MODEL_VERSION", "unset"))
    bar_freq = str(getattr(module, "BAR_FREQUENCY", "1d"))
    lookback_days = int(getattr(module, "LOOKBACK_DAYS", 252))
    bar_size = _BAR_FREQ_TO_SIZE.get(bar_freq)
    if bar_size is None:
        return {"status": "error", "error": f"unsupported BAR_FREQUENCY={bar_freq!r}; allowed={sorted(_BAR_FREQ_TO_SIZE)}"}
    duration = f"{lookback_days} D"

    try:
        from data.massive_client import get_bars
        bars_response = await get_bars(symbol, bar_size, duration, "TRADES")
    except Exception as exc:
        return {"status": "error", "error": f"bar fetch failed: {type(exc).__name__}: {exc}"}
    bars = bars_response.get("bars", []) if isinstance(bars_response, dict) else bars_response
    if not bars:
        return {"status": "error", "error": f"no bars returned for {symbol} (bar_size={bar_size}, duration={duration})"}

    # Fetch declared cross-asset extras. Best-effort: a per-symbol fetch failure
    # leaves an empty list under that key, the model decides whether it can
    # proceed. We don't short-circuit the whole conviction on one bad extra.
    extra_symbols = list(getattr(module, "EXTRA_SYMBOLS", []) or [])
    extra_bars: dict[str, list[dict]] = {}
    for extra in extra_symbols:
        try:
            resp = await get_bars(extra, bar_size, duration, "TRADES")
            eb = resp.get("bars", []) if isinstance(resp, dict) else (resp or [])
            extra_bars[extra] = eb or []
        except Exception as exc:
            log.warning(
                "conviction_from_model: extra_bars fetch failed agent=%s model=%s extra=%s err=%s",
                agent_name, model_name, extra, exc,
            )
            extra_bars[extra] = []

    if nav is None or regime is None:
        try:
            from ibkr.account import get_account_summary
            summary = await get_account_summary()
            if nav is None:
                nav = summary.get("nav")
        except Exception:
            pass
        if regime is None:
            try:
                mike_path = Path("data/mike_analysis") / f"{_market_date()}.json"
                if mike_path.exists():
                    regime = (json.loads(mike_path.read_text(encoding="utf-8")) or {}).get("regime")
            except Exception:
                pass
    context = {"nav": nav, "regime": regime, "agent_name": agent_name, "extra_bars": extra_bars}

    try:
        result = module.compute(symbol, bars, context)
    except Exception as exc:
        return {"status": "error", "error": f"model crashed: {type(exc).__name__}: {exc}"}
    if not isinstance(result, dict):
        return {"status": "error", "error": f"model returned non-dict: {type(result).__name__}"}

    signal = result.get("signal")
    direction = result.get("direction")
    if signal is None or direction in (None, "flat"):
        return {
            "status": "skipped",
            "reason": result.get("reason") or f"signal={signal} direction={direction}",
            "model_version": model_version,
        }
    if direction not in ("long", "short"):
        return {"status": "error", "error": f"model produced invalid direction={direction!r}"}

    try:
        conviction = float(result.get("conviction", 0.0))
        expected_return_pct = float(result["expected_return_pct"])
        time_to_target_days = int(result["time_to_target_days"])
    except (KeyError, TypeError, ValueError) as exc:
        return {"status": "error", "error": f"model output missing/invalid numeric field: {type(exc).__name__}: {exc}"}
    if time_to_target_days <= 0:
        return {"status": "error", "error": "model produced time_to_target_days <= 0 with non-flat direction"}
    if direction == "long" and expected_return_pct < 0:
        return {"status": "error", "error": f"contract violation: direction='long' but expected_return_pct={expected_return_pct} < 0"}
    if direction == "short" and expected_return_pct > 0:
        return {"status": "error", "error": f"contract violation: direction='short' but expected_return_pct={expected_return_pct} > 0"}

    stop_pct = result.get("stop_pct")
    if stop_pct is not None:
        try:
            stop_pct = float(stop_pct)
        except (TypeError, ValueError):
            stop_pct = None

    model_inputs = result.get("inputs") or {}
    if not isinstance(model_inputs, dict):
        model_inputs = {}
    # Stamp provenance into the inputs payload.
    model_inputs = {**model_inputs, "_model": model_name, "_version": model_version}

    return {
        "status": "ok",
        "payload": {
            "direction": direction,
            "conviction": conviction,
            "expected_return_pct": expected_return_pct,
            "time_to_target_days": time_to_target_days,
            "stop_pct": stop_pct,
            "model_inputs": model_inputs,
        },
        "model_version": model_version,
    }


def discover_agent_models(agent_name: str) -> list[dict[str, Any]]:
    """Return per-model metadata for an agent's models/ directory: name, version,
    BAR_FREQUENCY, LOOKBACK_DAYS, and first docstring line. Used by bundlers to
    advertise available models in the review prompt.
    """
    from meta_agent.model_loader import discover_models
    out: list[dict[str, Any]] = []
    for name in discover_models(agent_name):
        try:
            module = importlib.import_module(f"agents.{agent_name}.models.{name}")
            module = importlib.reload(module)
        except Exception as exc:
            out.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        doc = (getattr(module, "__doc__", None) or "").strip()
        doc_first_line = doc.split("\n", 1)[0] if doc else ""
        out.append({
            "name": name,
            "version": str(getattr(module, "MODEL_VERSION", "unset")),
            "bar_frequency": str(getattr(module, "BAR_FREQUENCY", "1d")),
            "lookback_days": int(getattr(module, "LOOKBACK_DAYS", 252)),
            "extra_symbols": list(getattr(module, "EXTRA_SYMBOLS", []) or []),
            "description": doc_first_line,
        })
    return out
