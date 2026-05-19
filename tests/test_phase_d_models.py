"""Contract + smoke tests for the Phase-D model trio:
roll_reg_momentum, garch_drift, news_bayesian. Each must follow MODEL_CONTRACT
and emit validator-clean distributions on synthetic bars."""
from __future__ import annotations

import importlib
import math
import random

from meta_agent.distribution_validator import validate_distribution


def _build_bars(n: int = 80, base: float = 100.0, drift: float = 0.0,
                vol: float = 0.005, seed: int = 11) -> list[dict]:
    rnd = random.Random(seed)
    price = base
    bars: list[dict] = []
    for _ in range(n):
        ret = drift + vol * (2 * rnd.random() - 1)
        new_price = max(0.01, price * math.exp(ret))
        bars.append({"o": price, "h": max(price, new_price) * 1.001,
                     "l": min(price, new_price) * 0.999, "c": new_price,
                     "v": 1_000_000})
        price = new_price
    return bars


# ── roll_reg_momentum ───────────────────────────────────────────────────────

def test_roll_reg_momentum_distributions_validate():
    m = importlib.import_module("agents.atlas.models.roll_reg_momentum")
    out = m.compute("AAPL", _build_bars(80, drift=0.001), {})
    assert out["distributions"]
    horizons = set()
    for d in out["distributions"]:
        ok, reason = validate_distribution(d)
        assert ok, f"{d.get('horizon')}: {reason}"
        horizons.add(d["horizon"])
    assert {"1h", "1d"}.issubset(horizons)


def test_roll_reg_momentum_skipped_on_short_bars():
    m = importlib.import_module("agents.atlas.models.roll_reg_momentum")
    out = m.compute("AAPL", _build_bars(10), {})
    assert out["signal"] is None


def test_roll_reg_momentum_inputs_stable():
    m = importlib.import_module("agents.atlas.models.roll_reg_momentum")
    out = m.compute("AAPL", _build_bars(80), {})
    assert set(out["inputs"].keys()) == {
        "ols_alpha", "ols_beta", "ols_sigma_resid", "window_bars", "last_close",
    }


# ── garch_drift ─────────────────────────────────────────────────────────────

def test_garch_drift_emits_four_horizons():
    m = importlib.import_module("agents.atlas.models.garch_drift")
    out = m.compute("AAPL", _build_bars(80), {})
    horizons = {d["horizon"] for d in out["distributions"]}
    assert horizons == {"5m", "1h", "1d", "1w"}


def test_garch_drift_distributions_validate():
    m = importlib.import_module("agents.atlas.models.garch_drift")
    out = m.compute("AAPL", _build_bars(80), {})
    for d in out["distributions"]:
        ok, reason = validate_distribution(d)
        assert ok, f"{d.get('horizon')}: {reason}"


def test_garch_drift_inputs_stable():
    m = importlib.import_module("agents.atlas.models.garch_drift")
    out = m.compute("AAPL", _build_bars(80), {})
    assert set(out["inputs"].keys()) == {
        "ewma_sigma_per_bar", "drift_per_bar", "ewma_lambda", "drift_window", "last_close",
    }


def test_garch_drift_variance_grows_with_horizon():
    """EWMA model — cumulative variance should grow with horizon. Compare
    bin spans (∝ σ) across the 5m vs 1w forecast."""
    m = importlib.import_module("agents.atlas.models.garch_drift")
    out = m.compute("AAPL", _build_bars(80), {})
    by_h = {d["horizon"]: d for d in out["distributions"]}
    span_5m = by_h["5m"]["bins"][-1]["x"] - by_h["5m"]["bins"][0]["x"]
    span_1w = by_h["1w"]["bins"][-1]["x"] - by_h["1w"]["bins"][0]["x"]
    assert span_1w > span_5m


# ── news_bayesian ───────────────────────────────────────────────────────────

def test_news_bayesian_with_empty_features_collapses_to_prior():
    """No news_features in context → posterior drift ≈ 0 (kappa × 0 = 0)."""
    m = importlib.import_module("agents.atlas.models.news_bayesian")
    out = m.compute("AAPL", _build_bars(80), {})
    for d in out["distributions"]:
        xs = [b["x"] for b in d["bins"]]
        ps = [b["p"] for b in d["bins"]]
        mu = sum(x * p for x, p in zip(xs, ps))
        assert abs(mu) < 0.5  # near-zero mean


def test_news_bayesian_with_positive_sentiment_shifts_positive():
    m = importlib.import_module("agents.atlas.models.news_bayesian")
    nf = {
        "recency_weighted_sentiment": 0.8,
        "max_importance_score": 1.0,
        "time_since_last_high_importance_min": 30.0,
        "count_earnings": 1,
        "count_guidance": 0,
        "count_m_and_a": 0,
        "count_regulatory": 0,
        "count_analyst_ratings": 0,
        "count_other": 0,
        "window_minutes": 240,
        "computed_at": "2026-05-16T17:30:00+00:00",
    }
    out = m.compute("AAPL", _build_bars(80), {"news_features": nf})
    longest = out["distributions"][-1]
    xs = [b["x"] for b in longest["bins"]]
    ps = [b["p"] for b in longest["bins"]]
    mu = sum(x * p for x, p in zip(xs, ps))
    assert mu > 0
    assert out["direction"] == "long"


def test_news_bayesian_distributions_validate():
    m = importlib.import_module("agents.atlas.models.news_bayesian")
    out = m.compute("AAPL", _build_bars(80), {})
    for d in out["distributions"]:
        ok, reason = validate_distribution(d)
        assert ok, f"{d.get('horizon')}: {reason}"


def test_news_bayesian_inputs_stable():
    m = importlib.import_module("agents.atlas.models.news_bayesian")
    out = m.compute("AAPL", _build_bars(80), {})
    assert set(out["inputs"].keys()) == {
        "news_sentiment", "importance_boost", "news_signal",
        "ewma_sigma_per_bar", "last_close",
    }
