"""Ornstein-Uhlenbeck mean-reversion forecast.

Fits log-return AR(1) on the last 60 five-minute bars (≈ one trading day at
RTH cadence). The AR(1) coefficient is the discrete equivalent of the OU
mean-reversion speed θ; residual variance is the diffusion σ².

Emits a probabilistic distribution per horizon (5m, 1h) as a discretized
Gaussian over return_pct relative to the current close. Bins are uniformly
spaced across ±3σ_h with 11 bins; bin probabilities come from CDF differences
of the Gaussian at the per-bin edges, then renormalized to sum to 1 with the
smoothing floor (p ≥ 1e-4) the validator requires.

Designed for: post-shock bounce thesis on a single symbol with no sectoral
spread. Mean-reverts a short-term dislocation; does not call directionally
on its own — direction is read off the sign of E[r] at the longest horizon
the model emits.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from statistics import mean
from typing import Any

MODEL_VERSION = "0.1.0"
BAR_FREQUENCY = "5m"
LOOKBACK_DAYS = 1            # ~60 5-min bars in RTH ≈ one trading day
MIN_BARS = 30
EXTRA_SYMBOLS: list[str] = []

# Horizons we forecast. In minutes; mapped to distribution.horizon labels.
_HORIZONS_MIN = [("5m", 5), ("1h", 60)]
_BAR_MINUTES = 5             # corresponds to BAR_FREQUENCY = "5m"
_N_BINS = 11                 # uniform; satisfies validator (3 ≤ n ≤ 20)
_BIN_SIGMA_SPAN = 3.0        # bins cover ±3σ
_P_FLOOR = 1.0e-4            # validator smoothing floor


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


def _ar1_fit(returns: list[float]) -> tuple[float, float, float]:
    """OLS fit r_t = α + β r_{t-1} + ε. Returns (alpha, beta, sigma_resid).
    Equivalent to OU when β ∈ (0, 1): θ ≈ -log(β)/Δt; long-run mean = α/(1-β).
    """
    if len(returns) < 3:
        return 0.0, 0.0, 0.0
    xs = returns[:-1]
    ys = returns[1:]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    beta = (num / den) if den > 0 else 0.0
    alpha = my - beta * mx
    resid = [y - (alpha + beta * x) for x, y in zip(xs, ys)]
    if n > 2:
        var_resid = sum(e * e for e in resid) / (n - 2)
    else:
        var_resid = 0.0
    return alpha, beta, math.sqrt(max(var_resid, 0.0))


def _forecast_mean_var(alpha: float, beta: float, sigma_resid: float,
                       r0: float, steps: int) -> tuple[float, float]:
    """Project AR(1) k steps ahead. Returns (E[r_t+k − r_t], Var[…]).
    Cumulative log-return over `steps` bars."""
    if steps <= 0:
        return 0.0, 0.0
    if abs(beta) >= 1.0:
        # Non-stationary fit — bail out; caller will see flat distribution.
        return 0.0, sigma_resid * sigma_resid * steps
    r = r0
    cum_mean = 0.0
    cum_var = 0.0
    # var of cumulative sum of AR(1) shocks; closed-form would be cleaner but
    # this is k <= 12 so the loop is cheap and explicit.
    for _ in range(steps):
        r_next_mean = alpha + beta * r
        cum_mean += r_next_mean
        cum_var += sigma_resid * sigma_resid
        r = r_next_mean
    return cum_mean, cum_var


def _build_distribution(mean_log_ret: float, std_log_ret: float,
                        anchor_price: float, horizon_label: str) -> dict:
    """Convert (mean, std) of cumulative log-return into a discretized
    distribution over return_pct (percent). Uniform bins across ±3σ centered
    on the projected mean; CDF differences give per-bin probabilities."""
    # Convert log-return params to percent-return params (small-x linear
    # approximation: %ret ≈ 100 · (e^logret − 1) ≈ 100 · logret for |logret|<<1).
    mu_pct = 100.0 * mean_log_ret
    sigma_pct = 100.0 * std_log_ret
    sigma_pct = max(sigma_pct, 0.05)  # floor so bins always span a meaningful range

    span = _BIN_SIGMA_SPAN * sigma_pct
    # Pick an exact spacing first, then derive centers as integer multiples.
    # Otherwise rounding the per-center value to 4 decimals breaks the validator's
    # uniform-spacing requirement (max relative deviation 1e-6).
    spacing = round((2.0 * span) / (_N_BINS - 1), 8)
    lo = round(mu_pct - span, 8)
    centers = [round(lo + i * spacing, 8) for i in range(_N_BINS)]
    # Probability of each bin = CDF(bin_upper_edge) − CDF(bin_lower_edge),
    # using bin half-width on either side of each center.
    half = spacing / 2.0
    probs: list[float] = []
    for c in centers:
        z_hi = (c + half - mu_pct) / sigma_pct
        z_lo = (c - half - mu_pct) / sigma_pct
        probs.append(_phi_cdf(z_hi) - _phi_cdf(z_lo))

    # Smooth then renormalize. Validator requires p ≥ 1e-4 and sum ≈ 1.
    probs = [max(p, _P_FLOOR) for p in probs]
    s = sum(probs)
    probs = [p / s for p in probs]

    bins = [{"x": c, "p": round(p, 6)} for c, p in zip(centers, probs)]
    return {
        "anchor_price": round(anchor_price, 4),
        "anchor_ts": datetime.now(timezone.utc).isoformat(),
        "axis": "return_pct",
        "horizon": horizon_label,
        "bins": bins,
        "model": "ou_mean_revert",
        "model_version": MODEL_VERSION,
    }


def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if len(bars) < MIN_BARS:
        return _no_signal(f"need >={MIN_BARS} bars, got {len(bars)}")

    closes = [float(b["c"]) for b in bars if b.get("c") is not None]
    if len(closes) < MIN_BARS:
        return _no_signal(f"insufficient close prices, got {len(closes)}")

    # Log returns r_t = log(c_t / c_{t-1}). Drop the first NaN.
    log_returns: list[float] = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0:
            continue
        log_returns.append(math.log(closes[i] / closes[i - 1]))
    if len(log_returns) < MIN_BARS - 1:
        return _no_signal("insufficient log returns after cleanup")

    alpha, beta, sigma_resid = _ar1_fit(log_returns)
    if sigma_resid == 0 or beta >= 1.0:
        return _no_signal(
            f"degenerate AR(1): beta={beta:.4f} sigma_resid={sigma_resid:.6f}"
        )

    last_close = closes[-1]
    last_log_ret = log_returns[-1] if log_returns else 0.0

    distributions = []
    longest_mu = 0.0
    for label, minutes in _HORIZONS_MIN:
        steps = max(1, minutes // _BAR_MINUTES)
        cum_mean, cum_var = _forecast_mean_var(alpha, beta, sigma_resid, last_log_ret, steps)
        cum_std = math.sqrt(max(cum_var, 0.0))
        distributions.append(_build_distribution(cum_mean, cum_std, last_close, label))
        longest_mu = cum_mean  # last loop iteration = longest horizon

    # Direction comes from the sign of the longest-horizon expected log-return.
    # Magnitude check: require at least 0.1% expected move to take a direction.
    pct = 100.0 * longest_mu
    if abs(pct) < 0.1:
        direction = "flat"
        e_return = 0.0
        ttd = 0
    else:
        direction = "long" if pct > 0 else "short"
        e_return = round(pct, 3)
        ttd = 1  # 1h horizon expressed as a 1-day ttt for the int column

    # AR(1) inputs the model literally used. Stable keys for the
    # model_inputs_validator registry.
    inputs = {
        "ar1_alpha":  round(alpha, 6),
        "ar1_beta":   round(beta, 6),
        "sigma_resid": round(sigma_resid, 6),
        "last_log_return": round(last_log_ret, 6),
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
            f"ou_mean_revert: 1h E[r]={pct:.2f}% σ_resid={sigma_resid:.4f} β={beta:.3f}"
        ),
    }
