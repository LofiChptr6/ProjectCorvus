"""News-Bayesian forecast: prior = vol-anchored Gaussian; likelihood = news
event vector; posterior = mean-shifted, variance-inflated.

Conceptually:
  Prior:      r_h ~ N(0, σ²·h)   where σ² is EWMA-vol on bars
  Likelihood: news_signal = recency_weighted_sentiment + bias from event counts
  Posterior: r_h ~ N(news_signal · h · κ, σ²·h · (1 + η·|news_signal|))

κ is the sentiment→drift gain (1bp per recency-weighted-sentiment unit per
horizon-bar — a small-default that the calibration loop should A/B against
larger values). η inflates variance when news disagrees with the prior or
piles up at high importance — earnings/macro shocks broaden the distribution.

Self-contained: does not chain to another model's output. Composition with
roll_reg_momentum / garch_drift can come in a future iteration once the
news-features cadence is validated end-to-end.

Reads `context["news_features"]` (populated by analysis/news_features.py
precompute job). Falls back to an empty feature vector when the snapshot
table is bare — in which case the posterior just equals the prior.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from analysis.news_features import empty_features

MODEL_VERSION = "0.1.0"
BAR_FREQUENCY = "5m"
LOOKBACK_DAYS = 1
MIN_BARS = 30
EXTRA_SYMBOLS: list[str] = []

# Horizons in bars
_HORIZONS_BARS = [("1h", 12), ("1d", 78), ("1w", 78 * 5)]
_EWMA_LAMBDA = 0.97
_SENT_GAIN_KAPPA = 0.0005       # 1 unit of recency-weighted sentiment → 5bp drift per bar
_VAR_INFLATION_ETA = 0.5        # variance scales by (1 + η · |news_signal|)
_HIGH_IMPORTANCE_BIAS = 0.3     # if a high-importance event landed recently, add this to |news_signal|
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


def _ewma_variance(returns: list[float], lam: float) -> float:
    if len(returns) < 11:
        m = sum(returns) / len(returns)
        return sum((r - m) ** 2 for r in returns) / max(len(returns) - 1, 1)
    seed = returns[:10]
    m = sum(seed) / len(seed)
    var = sum((r - m) ** 2 for r in seed) / max(len(seed) - 1, 1)
    for r in returns[10:]:
        var = (1.0 - lam) * r * r + lam * var
    return var


def _news_signal(features: dict) -> tuple[float, float]:
    """Return (drift_signal, importance_boost).

    drift_signal: signed [-1, 1]-ish; positive = bullish.
    importance_boost: 0 if no recent high-importance items, ~1 if a high-importance
                      item arrived within the past hour, decaying by half each hour.
    """
    sentiment = float(features.get("recency_weighted_sentiment") or 0.0)
    max_imp = float(features.get("max_importance_score") or 0.0)
    time_since = features.get("time_since_last_high_importance_min")
    if time_since is None or time_since < 0:
        importance_boost = 0.0
    else:
        # half-life 60 minutes
        importance_boost = max_imp * math.pow(0.5, time_since / 60.0)
    return sentiment, importance_boost


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
        "model": "news_bayesian",
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
    if not log_returns:
        return _no_signal("no log returns")

    # Prior variance via EWMA.
    prior_var = _ewma_variance(log_returns, _EWMA_LAMBDA)
    sigma_per_bar = math.sqrt(max(prior_var, 0.0))
    if sigma_per_bar == 0:
        return _no_signal("degenerate prior: zero EWMA vol")

    # News features. If the precompute hasn't run yet, fall back to empty —
    # posterior = prior in that case (i.e. zero-mean Gaussian).
    features = (context or {}).get("news_features") or empty_features()
    sentiment, importance_boost = _news_signal(features)
    news_signal = sentiment + math.copysign(importance_boost, sentiment or 1.0)

    last_close = closes[-1]
    distributions = []
    longest_mu = 0.0
    for label, bars_ahead in _HORIZONS_BARS:
        # Posterior drift = sentiment-driven per-bar shift, cumulative across horizon.
        cum_mean = _SENT_GAIN_KAPPA * news_signal * bars_ahead
        # Posterior variance inflated by news disagreement / pileup.
        inflation = 1.0 + _VAR_INFLATION_ETA * abs(news_signal)
        cum_std = sigma_per_bar * math.sqrt(bars_ahead * inflation)
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
        ttd = 7

    inputs = {
        "news_sentiment":       round(sentiment, 4),
        "importance_boost":     round(importance_boost, 4),
        "news_signal":          round(news_signal, 4),
        "ewma_sigma_per_bar":   round(sigma_per_bar, 8),
        "last_close":           round(last_close, 4),
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
            f"news_bayesian: 1w E[r]={pct:.2f}% signal={news_signal:+.3f}"
        ),
    }
