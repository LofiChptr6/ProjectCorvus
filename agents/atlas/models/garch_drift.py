"""EWMA-vol + drift forecast (RiskMetrics-style variance proxy).

True GARCH(1,1) MLE — σ²_t = ω + α·ε²_{t-1} + β·σ²_{t-1} — requires a
constrained optimizer and is finicky on short windows. As a v1 stand-in we
ship EWMA-vol (the RiskMetrics convention): σ²_t = (1-λ)·r²_{t-1} + λ·σ²_{t-1}
with λ=0.94 for daily, 0.97 for intraday. Same conditional-heteroskedasticity
spirit, one fewer parameter, deterministic fit.

Forecast: drift = simple recent mean of log-returns; cumulative h-step variance
= h · σ²_t (no mean-reversion of σ across the horizon; conservative for short
horizons, slightly pessimistic for long ones). Discretized as a Gaussian over
return_pct.

When/why upgrade: if cross-validation shows EWMA systematically under- or
over-predicts vol relative to realized, drop in a full ARMA+GARCH via the
`arch` library (not currently a dep — Python 3.14 wheels aren't published yet).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

MODEL_VERSION = "0.1.0"
BAR_FREQUENCY = "5m"
LOOKBACK_DAYS = 1
MIN_BARS = 30
EXTRA_SYMBOLS: list[str] = []

# Horizons in bars (1 bar = 5 min)
_HORIZONS_BARS = [("5m", 1), ("1h", 12), ("1d", 78), ("1w", 78 * 5)]
_BAR_MINUTES = 5
_EWMA_LAMBDA = 0.97          # intraday RiskMetrics; lower than 0.94 to be more reactive
_DRIFT_WINDOW = 30           # last 30 5-min bars for mean log-return
_N_BINS = 11
_BIN_SIGMA_SPAN = 3.0
_P_FLOOR = 1.0e-4


def _no_signal(reason: str) -> dict[str, Any]:
    return {
        "signal": None,
        "direction": None,
        "conviction": 0.0,
        "expected_return_pct": 0.0,
        "time_to_target_days": 0,
        "inputs": {},
        "reason": reason,
    }


def _phi_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _ewma_variance(returns: list[float], lam: float) -> float:
    """RiskMetrics EWMA variance: σ²_t = (1-λ)·r²_{t-1} + λ·σ²_{t-1}, seeded
    with the unconditional sample variance over the first 10 returns."""
    if len(returns) < 11:
        # Fall back to sample variance.
        m = sum(returns) / len(returns)
        return sum((r - m) ** 2 for r in returns) / max(len(returns) - 1, 1)
    seed = returns[:10]
    m = sum(seed) / len(seed)
    var = sum((r - m) ** 2 for r in seed) / max(len(seed) - 1, 1)
    for r in returns[10:]:
        var = (1.0 - lam) * r * r + lam * var
    return var


def _build_distribution(mean_log_ret: float, std_log_ret: float,
                        anchor_price: float, horizon_label: str) -> dict:
    mu_pct = 100.0 * mean_log_ret
    sigma_pct = max(100.0 * std_log_ret, 0.05)
    span = _BIN_SIGMA_SPAN * sigma_pct
    spacing = round((2.0 * span) / (_N_BINS - 1), 8)
    lo = round(mu_pct - span, 8)
    centers = [round(lo + i * spacing, 8) for i in range(_N_BINS)]
    half = spacing / 2.0
    probs: list[float] = []
    for c in centers:
        z_hi = (c + half - mu_pct) / sigma_pct
        z_lo = (c - half - mu_pct) / sigma_pct
        probs.append(_phi_cdf(z_hi) - _phi_cdf(z_lo))
    probs = [max(p, _P_FLOOR) for p in probs]
    s = sum(probs)
    probs = [p / s for p in probs]
    return {
        "anchor_price": round(anchor_price, 4),
        "anchor_ts": datetime.now(timezone.utc).isoformat(),
        "axis": "return_pct",
        "horizon": horizon_label,
        "bins": [{"x": c, "p": round(p, 6)} for c, p in zip(centers, probs)],
        "model": "garch_drift",
        "model_version": MODEL_VERSION,
    }


def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if len(bars) < MIN_BARS:
        return _no_signal(f"need >={MIN_BARS} bars, got {len(bars)}")

    closes = [float(b["c"]) for b in bars if b.get("c") is not None and float(b["c"]) > 0]
    if len(closes) < MIN_BARS:
        return _no_signal(f"insufficient positive closes, got {len(closes)}")

    log_returns: list[float] = []
    for i in range(1, len(closes)):
        log_returns.append(math.log(closes[i] / closes[i - 1]))
    if len(log_returns) < MIN_BARS - 1:
        return _no_signal("insufficient log returns")

    sigma2 = _ewma_variance(log_returns, _EWMA_LAMBDA)
    sigma_per_bar = math.sqrt(max(sigma2, 0.0))
    if sigma_per_bar == 0:
        return _no_signal("degenerate EWMA: zero vol")

    drift_window = log_returns[-_DRIFT_WINDOW:]
    drift_per_bar = sum(drift_window) / len(drift_window)
    last_close = closes[-1]

    distributions = []
    longest_mu = 0.0
    for label, bars_ahead in _HORIZONS_BARS:
        cum_mean = drift_per_bar * bars_ahead
        cum_std = sigma_per_bar * math.sqrt(bars_ahead)
        distributions.append(_build_distribution(cum_mean, cum_std, last_close, label))
        longest_mu = cum_mean

    pct = 100.0 * longest_mu
    if abs(pct) < 0.1:
        direction = "flat"
        e_return = 0.0
        ttd = 0
    else:
        direction = "long" if pct > 0 else "short"
        e_return = round(pct, 3)
        ttd = 7  # longest horizon is 1w

    inputs = {
        "ewma_sigma_per_bar": round(sigma_per_bar, 8),
        "drift_per_bar":      round(drift_per_bar, 8),
        "ewma_lambda":        _EWMA_LAMBDA,
        "drift_window":       len(drift_window),
        "last_close":         round(last_close, 4),
    }

    return {
        "signal": round(pct, 3),
        "direction": direction,
        "conviction": min(abs(pct) / 5.0, 1.0) if direction != "flat" else 0.0,
        "expected_return_pct": e_return,
        "time_to_target_days": ttd,
        "stop_pct": None,
        "inputs": inputs,
        "distributions": distributions,
        "interpretation": (
            f"garch_drift: 1w E[r]={pct:.2f}% σ_bar={sigma_per_bar:.5f} drift={drift_per_bar:.5f}"
        ),
    }
