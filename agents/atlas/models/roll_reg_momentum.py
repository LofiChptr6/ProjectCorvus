"""Rolling-window OLS momentum forecast.

Fits log(price) ~ α + β·t over the most recent N bars and projects forward.
The drift comes from β (slope per bar); the variance comes from residual
σ² and grows linearly with horizon (random-walk-around-trend assumption).

Designed for: trend continuation theses on a single symbol. Complements
ou_mean_revert (which assumes a mean-reverting AR(1) shock) by taking the
opposite stance — recent direction persists.

Emits a probabilistic distribution per horizon (1h, 1d) discretized over
return_pct relative to the current close.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

MODEL_VERSION = "0.1.0"
BAR_FREQUENCY = "1h"
LOOKBACK_DAYS = 14            # ~70 trading-hour bars per 10d window
MIN_BARS = 24
EXTRA_SYMBOLS: list[str] = []

_HORIZONS_HOURS = [("1h", 1), ("1d", 7)]  # 1d ≈ 7 RTH hours
_BAR_MINUTES = 60
_N_BINS = 11
_BIN_SIGMA_SPAN = 3.0
_P_FLOOR = 1.0e-4


def _no_signal(reason: str) -> dict[str, Any]:
    return {
        "signal": None,
        "direction": None,
        "likelihood": 0.0,
        "expected_return_pct": 0.0,
        "time_to_target_days": 0,
        "inputs": {},
        "reason": reason,
    }


def _phi_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _ols_slope_intercept(t_vals: list[float], y_vals: list[float]) -> tuple[float, float, float]:
    """OLS y = α + β·t; return (alpha, beta, sigma_resid)."""
    n = len(t_vals)
    if n < 3:
        return 0.0, 0.0, 0.0
    mt = sum(t_vals) / n
    my = sum(y_vals) / n
    num = sum((t - mt) * (y - my) for t, y in zip(t_vals, y_vals))
    den = sum((t - mt) ** 2 for t in t_vals)
    beta = num / den if den > 0 else 0.0
    alpha = my - beta * mt
    resid = [y - (alpha + beta * t) for t, y in zip(t_vals, y_vals)]
    if n > 2:
        var_resid = sum(e * e for e in resid) / (n - 2)
    else:
        var_resid = 0.0
    return alpha, beta, math.sqrt(max(var_resid, 0.0))


def _build_distribution(mean_log_ret: float, std_log_ret: float,
                        anchor_price: float, horizon_label: str) -> dict:
    """Discretize a Gaussian (μ, σ) on log-return into uniformly-spaced
    return_pct bins. Identical math to ou_mean_revert._build_distribution
    — kept inline rather than factored so each model owns its full I/O contract
    (a refactor target once 4 models share the pattern)."""
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
        "model": "roll_reg_momentum",
        "model_version": MODEL_VERSION,
    }


def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if len(bars) < MIN_BARS:
        return _no_signal(f"need >={MIN_BARS} bars, got {len(bars)}")

    closes = [float(b["c"]) for b in bars if b.get("c") is not None and float(b["c"]) > 0]
    if len(closes) < MIN_BARS:
        return _no_signal(f"insufficient positive closes, got {len(closes)}")

    # Use last MIN_BARS for the regression to keep the model responsive to
    # recent regime changes rather than over-smoothing across distant history.
    window = closes[-MIN_BARS:]
    log_prices = [math.log(c) for c in window]
    t_vals = list(range(len(window)))
    alpha, beta, sigma_resid = _ols_slope_intercept(t_vals, log_prices)

    if sigma_resid <= 0:
        return _no_signal(f"degenerate fit: sigma_resid={sigma_resid:.6f}")

    last_close = closes[-1]
    distributions = []
    longest_mu = 0.0
    for label, hours in _HORIZONS_HOURS:
        # β is per-bar drift in log-price. Horizon expressed in bars (= hours
        # since BAR_FREQUENCY='1h'). Cumulative drift = β · hours.
        cum_mean = beta * hours
        # Random-walk-around-trend: var of cumulative residual = σ² · hours
        cum_std = sigma_resid * math.sqrt(hours)
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
        ttd = 1

    inputs = {
        "ols_alpha":  round(alpha, 6),
        "ols_beta":   round(beta, 8),
        "ols_sigma_resid": round(sigma_resid, 6),
        "window_bars": len(window),
        "last_close": round(last_close, 4),
    }

    return {
        "signal": round(pct, 3),
        "direction": direction,
        "likelihood": min(abs(pct) / 5.0, 1.0) if direction != "flat" else 0.0,
        "expected_return_pct": e_return,
        "time_to_target_days": ttd,
        "stop_pct": None,
        "inputs": inputs,
        "distributions": distributions,
        "interpretation": (
            f"roll_reg_momentum: 1d E[r]={pct:.2f}% β={beta:.5f} σ_resid={sigma_resid:.4f}"
        ),
    }
