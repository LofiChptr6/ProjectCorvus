"""Shared helper: turn a quant model into a conviction-ready payload.

Two call sites use this:
  - mcp_server.submit_conviction_from_model (one-shot tool)
  - pipelines.runner._apply_review_output  (batched per-review when a
    ConvictionView carries `from_model`)

The helper handles model lifecycle (discover → import → run → validate output
against MODEL_CONTRACT.md) and — when the model emits `distributions` — also
validates + persists them on agent_forecast with a fresh forecast_run_id
shared with the returned scalar conviction. It does NOT write to
agent_conviction, NOT check rate-limits, NOT check sector ownership — those
remain caller responsibilities.

See agents/MODEL_CONTRACT.md for the compute() output spec.
"""
from __future__ import annotations

import importlib
import json
import logging
import uuid
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

    # Likelihood — the model's probability in [0, 1] that the forecast plays
    # out. All 16 active models emit `likelihood` directly per MODEL_CONTRACT.md
    # (rev 2026-05-20). The legacy `conviction`-named field was dropped after
    # the migration; if a future model accidentally emits `conviction` and
    # omits `likelihood`, the row gets likelihood=0 and is skipped.
    try:
        likelihood = float(result.get("likelihood") or 0.0)
    except (TypeError, ValueError):
        likelihood = 0.0
    if likelihood < 0.0:
        likelihood = 0.0
    elif likelihood > 1.0:
        likelihood = 1.0

    # Central conviction calculation — same formula as submit_conviction_view.
    # If the distributions branch overrides this below, it wins.
    from meta_agent.allocator import compute_conviction
    conviction = compute_conviction(expected_return_pct, likelihood, time_to_target_days)

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

    # ── Probabilistic-forecast path ──────────────────────────────────────────
    # When the model emits `distributions`, validate them, persist one row per
    # horizon on agent_forecast under a fresh forecast_run_id, and (when a
    # conviction functional is enabled) override the scalar conviction with
    # the functional applied to the distributions. Models that do NOT emit
    # distributions fall through to the legacy scalar-only path unchanged.
    forecast_run_id: Optional[str] = None
    functional_name: Optional[str] = None
    distributions = result.get("distributions")
    if distributions:
        try:
            forecast_run_id, functional_name, conviction_override = await _persist_distributions(
                agent_name=agent_name,
                symbol=symbol,
                direction=direction,
                expected_return_pct=expected_return_pct,
                model_name=model_name,
                model_version=model_version,
                distributions=distributions,
            )
            if conviction_override is not None:
                conviction = conviction_override
        except ValueError as exc:
            return {"status": "error", "error": f"distribution validation failed: {exc}"}
        except Exception as exc:
            log.warning(
                "conviction_from_model: distribution persistence failed agent=%s model=%s symbol=%s err=%s",
                agent_name, model_name, symbol, exc,
            )
            # Continue with legacy scalar conviction; do NOT silently drop
            # the whole submission for a forecast-persist hiccup.

    return {
        "status": "ok",
        "payload": {
            "direction": direction,
            "conviction": conviction,
            "expected_return_pct": expected_return_pct,
            "time_to_target_days": time_to_target_days,
            "likelihood": likelihood,
            "stop_pct": stop_pct,
            "model_inputs": model_inputs,
            "forecast_run_id": forecast_run_id,
            "functional_name": functional_name,
        },
        "model_version": model_version,
    }


async def _persist_distributions(
    agent_name: str,
    symbol: str,
    direction: str,
    expected_return_pct: float,
    model_name: str,
    model_version: str,
    distributions: list,
) -> tuple[str, Optional[str], Optional[float]]:
    """Validate + persist a list of per-horizon distributions on agent_forecast
    under a single forecast_run_id. Return (run_id, functional_name,
    conviction_override).

    Each distribution must have anchor_price/anchor_ts/axis/horizon/bins/model/
    model_version and pass meta_agent.distribution_validator. Caller is the
    runner; symbol ownership + rate limits enforced upstream at the MCP layer.
    """
    from db import store
    from meta_agent import conviction_functionals
    from meta_agent.distribution_validator import (
        horizon_to_ttd_days,
        validate_distribution,
    )

    if not isinstance(distributions, list) or not distributions:
        raise ValueError("distributions must be a non-empty list")

    # distribution.horizon ∈ {5m, 1h, 1d, 1w} per MODEL_CONTRACT, but
    # agent_forecast.horizon is constrained to {5m, 1h, intraday, near, far,
    # cycle} — the dashboard's _HORIZON_ORDER tuple and bucket-based queries
    # break on raw 1d/1w. Map daily/weekly distribution horizons to their
    # bucket equivalents (1d→near, 1w→far). 5m/1h pass through unchanged
    # because the upsert allow-list includes them. The distribution jsonb
    # keeps the original label so the scorer/resolver still see "1d"/"1w".
    _DIST_HORIZON_TO_BUCKET = {"5m": "5m", "1h": "1h", "1d": "near", "1w": "far"}

    run_id = str(uuid.uuid4())
    rows: list[dict] = []
    horizon_pairs: list[tuple[dict, float]] = []
    for i, dist in enumerate(distributions):
        if not isinstance(dist, dict):
            raise ValueError(f"distributions[{i}] not a dict")
        ok, reason = validate_distribution(dist)
        if not ok:
            raise ValueError(f"distributions[{i}] invalid: {reason}")
        horizon = dist["horizon"]
        row_horizon = _DIST_HORIZON_TO_BUCKET.get(horizon, horizon)
        ttd_days = horizon_to_ttd_days(horizon)
        # Mirror E[r] into the legacy expected_return_pct column for back-compat;
        # likelihood = peak(p) gives consumers a usable scalar without parsing
        # the full distribution.
        bins = dist["bins"]
        xs = [float(b["x"]) for b in bins]
        ps = [float(b["p"]) for b in bins]
        er = sum(x * p for x, p in zip(xs, ps))
        lk = max(ps) if ps else 0.0
        # Per-row expiration sized to the forecast horizon. Short horizons
        # need rapid refresh; long horizons can persist. ttd_days is the
        # horizon in trading days; allow ~1 trading session of TTL with a
        # 2h floor (matches the prior batch default) and a 30-day ceiling.
        ttl_hours = max(2.0, min(720.0, float(ttd_days) * 24.0 / 5.0))
        rows.append({
            "symbol": symbol,
            "expected_return_pct": er,
            "likelihood": lk,
            "time_to_target_days": ttd_days,
            "method": f"model:{model_name}@{model_version}",
            "rationale": None,
            "horizon": row_horizon,
            "distribution": dist,
            "forecast_run_id": run_id,
            "expires_in_hours": ttl_hours,
        })
        horizon_pairs.append((dist, float(ttd_days)))

    res = await store.upsert_forecasts_batch(
        agent_name=agent_name,
        rows=rows,
    )
    if res.get("errors"):
        raise ValueError(f"forecast upsert errors: {res['errors']}")

    # Per-agent functional choice (Phase G). Reads agents/<agent>.yaml's
    # `conviction_functional` field; falls back to DEFAULT_FUNCTIONAL when
    # unset or invalid. Allows data-driven A/B via
    # scripts/suggest_functional_per_agent.py.
    functional_name = conviction_functionals.functional_for_agent(agent_name)
    try:
        conviction_override = conviction_functionals.collapse_across_horizons(
            functional_name, horizon_pairs,
        )
    except Exception as exc:  # registry mis-config; non-fatal
        log.warning("conviction_functional %s failed: %s", functional_name, exc)
        conviction_override = None

    return run_id, functional_name, conviction_override


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
