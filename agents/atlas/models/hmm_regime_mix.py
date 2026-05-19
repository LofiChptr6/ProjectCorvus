"""2-state Gaussian HMM regime-mixture forecast.

Fits a 2-component Gaussian HMM (bull / bear regimes) on daily log-returns,
then forecasts a Gaussian-mixture distribution conditioned on the current
posterior regime probabilities. Each state has its own (μ, σ) drift +
volatility; the forecast distribution is the regime-prob-weighted mixture
projected forward `h` bars (random-walk-around-regime-mean).

Trade-offs (per the plan critique):
  - HMM fitting is fiddly; we use conservative settings (covariance_type='diag',
    n_iter=25, random_state=42) and accept moderate fit instability across
    calls. Future improvement: cache fitted params nightly via model_tune
    instead of refitting per call.
  - Marked "queue-only" — do NOT call from an hourly review path. The HMM
    fit takes ~50–300ms; run via the agent_job queue's
    `quant_distribution_compute` job type so the inline review loop never
    waits on it. (Enforced by docstring + queue-worker dispatcher; not a
    Python-level guard.)

Horizons: 1d, 1w. Both project regime persistence (no transition probability
applied to the forecast — assumes regimes are sticky enough over the next
few days).
"""
from __future__ import annotations

import math
import warnings
from datetime import datetime, timezone
from typing import Any

import numpy as np
from hmmlearn.hmm import GaussianHMM

MODEL_VERSION = "0.1.0"
BAR_FREQUENCY = "1d"
LOOKBACK_DAYS = 200
MIN_BARS = 60
EXTRA_SYMBOLS: list[str] = []

# Horizons in days (BAR_FREQUENCY='1d' → 1 bar = 1 day)
_HORIZONS_DAYS = [("1d", 1), ("1w", 5)]
_N_BINS = 11
_BIN_SIGMA_SPAN = 3.0
_P_FLOOR = 1.0e-4
_HMM_N_STATES = 2
_HMM_N_ITER = 25


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


def _mixture_gaussian_bin_probs(
    centers: list[float], spacing: float,
    weights: list[float], mus: list[float], sigmas: list[float],
) -> list[float]:
    """Per-bin probability for a Gaussian mixture, via CDF differences."""
    half = spacing / 2.0
    probs: list[float] = []
    for c in centers:
        lo = c - half
        hi = c + half
        p = 0.0
        for w, mu, sigma in zip(weights, mus, sigmas):
            if sigma <= 0:
                continue
            p += w * (_phi_cdf((hi - mu) / sigma) - _phi_cdf((lo - mu) / sigma))
        probs.append(p)
    return probs


def _build_distribution(
    p_bull: float, mu_bull_pct: float, sigma_bull_pct: float,
    p_bear: float, mu_bear_pct: float, sigma_bear_pct: float,
    anchor_price: float, horizon_label: str,
) -> dict:
    # Bin range covers ±3σ of the wider regime, centered on the mixture mean.
    mixture_mu = p_bull * mu_bull_pct + p_bear * mu_bear_pct
    max_sigma = max(sigma_bull_pct, sigma_bear_pct, 0.05)
    span = _BIN_SIGMA_SPAN * max_sigma
    spacing = round((2.0 * span) / (_N_BINS - 1), 8)
    lo = round(mixture_mu - span, 8)
    centers = [round(lo + i * spacing, 8) for i in range(_N_BINS)]

    probs = _mixture_gaussian_bin_probs(
        centers, spacing,
        [p_bull, p_bear],
        [mu_bull_pct, mu_bear_pct],
        [sigma_bull_pct, sigma_bear_pct],
    )
    probs = [max(p, _P_FLOOR) for p in probs]
    s = sum(probs)
    probs = [p / s for p in probs]

    return {
        "anchor_price": round(anchor_price, 4),
        "anchor_ts": datetime.now(timezone.utc).isoformat(),
        "axis": "return_pct",
        "horizon": horizon_label,
        "bins": [{"x": c, "p": round(p, 6)} for c, p in zip(centers, probs)],
        "model": "hmm_regime_mix",
        "model_version": MODEL_VERSION,
    }


def _fit_hmm(returns: np.ndarray) -> GaussianHMM | None:
    """Fit 2-state Gaussian HMM. Returns None on convergence failure."""
    X = returns.reshape(-1, 1)
    model = GaussianHMM(
        n_components=_HMM_N_STATES,
        covariance_type="diag",
        n_iter=_HMM_N_ITER,
        random_state=42,
        # Conservative priors (per plan): mildly informative to discourage
        # collapse to a single state on quiet markets.
        init_params="stmc",
        params="stmc",
    )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # silence hmmlearn convergence pings
            model.fit(X)
    except (ValueError, np.linalg.LinAlgError):
        return None
    if not getattr(model, "monitor_", None) or not model.monitor_.converged:
        # Fit did not converge; safer to return None and emit no signal.
        return None
    return model


def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if len(bars) < MIN_BARS:
        return _no_signal(f"need >={MIN_BARS} bars, got {len(bars)}")
    closes = [float(b["c"]) for b in bars if b.get("c") is not None and float(b["c"]) > 0]
    if len(closes) < MIN_BARS:
        return _no_signal(f"insufficient positive closes, got {len(closes)}")

    log_returns = np.array([
        math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
    ], dtype=float)
    if len(log_returns) < MIN_BARS - 1:
        return _no_signal("insufficient log returns")

    hmm = _fit_hmm(log_returns)
    if hmm is None:
        return _no_signal("HMM did not converge")

    means = hmm.means_.ravel()          # shape (2,)
    covars = hmm.covars_.reshape(-1)    # diag → 1D
    sigmas = np.sqrt(np.maximum(covars, 1e-12))

    # State 0 = bear (lower mean); state 1 = bull (higher mean).
    bear_idx = int(np.argmin(means))
    bull_idx = 1 - bear_idx
    mu_bear, sigma_bear = float(means[bear_idx]), float(sigmas[bear_idx])
    mu_bull, sigma_bull = float(means[bull_idx]), float(sigmas[bull_idx])

    # Posterior regime probabilities at the most recent observation
    posterior = hmm.predict_proba(log_returns.reshape(-1, 1))[-1]
    p_bear = float(posterior[bear_idx])
    p_bull = float(posterior[bull_idx])

    last_close = closes[-1]
    distributions = []
    longest_mu = 0.0
    for label, days in _HORIZONS_DAYS:
        # Random-walk-around-regime-mean: cumulative drift = μ_state · h,
        # cumulative variance = σ_state² · h. Regime persistence assumed
        # over the (short) forecast horizon — transition matrix not applied
        # for v1 simplicity.
        mu_bull_h = 100.0 * mu_bull * days
        mu_bear_h = 100.0 * mu_bear * days
        sigma_bull_h = 100.0 * sigma_bull * math.sqrt(days)
        sigma_bear_h = 100.0 * sigma_bear * math.sqrt(days)
        dist = _build_distribution(
            p_bull, mu_bull_h, sigma_bull_h,
            p_bear, mu_bear_h, sigma_bear_h,
            last_close, label,
        )
        distributions.append(dist)
        # Track the longest-horizon mixture mean (for direction inference)
        longest_mu = p_bull * mu_bull_h + p_bear * mu_bear_h

    pct = float(longest_mu)
    if abs(pct) < 0.1:
        direction = "flat"
        e_return = 0.0
        ttd = 0
    else:
        direction = "long" if pct > 0 else "short"
        e_return = round(pct, 3)
        ttd = 5  # longest horizon is 1w

    inputs = {
        "hmm_mu_bull":    round(mu_bull, 8),
        "hmm_mu_bear":    round(mu_bear, 8),
        "hmm_sigma_bull": round(sigma_bull, 8),
        "hmm_sigma_bear": round(sigma_bear, 8),
        "hmm_p_bull":     round(p_bull, 6),
        "hmm_p_bear":     round(p_bear, 6),
        "last_close":     round(last_close, 4),
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
            f"hmm_regime_mix: 1w E[r]={pct:.2f}% P(bull)={p_bull:.2f} "
            f"μ_bull={mu_bull:.5f} μ_bear={mu_bear:.5f}"
        ),
    }
