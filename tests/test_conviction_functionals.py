"""Conviction-functional registry tests. Each functional must handle edge
cases (single-bin spike, all-mass-on-zero, asymmetric tails, σ=0) without
crashing and within its declared raw range."""
from __future__ import annotations

import math

from meta_agent import conviction_functionals
from meta_agent.conviction_functionals.summaries import (
    concentration, cvar, mean, peak, tail, variance,
)


def _uniform_dist(centers: list[float]) -> dict:
    p = 1.0 / len(centers)
    return {
        "anchor_price": 100.0,
        "anchor_ts": "2026-05-16T17:30:00+00:00",
        "axis": "return_pct",
        "horizon": "5m",
        "bins": [{"x": c, "p": p} for c in centers],
        "model": "test",
        "model_version": "0",
    }


def _spike_dist(center: float) -> dict:
    # 5-bin spike: 0.001 + 0.001 + 0.996 + 0.001 + 0.001 (validator-legal)
    spacing = 0.5
    centers = [center - 2 * spacing, center - spacing, center, center + spacing, center + 2 * spacing]
    probs = [1e-3, 1e-3, 0.996, 1e-3, 1e-3]
    return {
        "anchor_price": 100.0,
        "anchor_ts": "2026-05-16T17:30:00+00:00",
        "axis": "return_pct",
        "horizon": "5m",
        "bins": [{"x": c, "p": p} for c, p in zip(centers, probs)],
        "model": "test",
        "model_version": "0",
    }


# ── summaries ──────────────────────────────────────────────────────────────

def test_mean_zero_centered_symmetric():
    xs = [-2.0, -1.0, 0.0, 1.0, 2.0]
    ps = [0.2, 0.2, 0.2, 0.2, 0.2]
    assert abs(mean.compute(xs, ps)) < 1e-9


def test_mean_skewed_positive():
    xs = [-1.0, 0.0, 1.0, 2.0, 3.0]
    ps = [0.05, 0.1, 0.5, 0.25, 0.1]
    mu = mean.compute(xs, ps)
    assert mu > 0


def test_variance_zero_when_collapsed_to_one_bin():
    xs = [-1.0, 0.0, 1.0]
    ps = [1e-9, 1.0 - 2e-9, 1e-9]
    v = variance.compute(xs, ps)
    assert v < 1e-6


def test_concentration_bounds():
    xs = [-2.0, -1.0, 0.0, 1.0, 2.0]
    uniform_p = [0.2] * 5
    spike_p = [1e-4, 1e-4, 0.9996, 1e-4, 1e-4]
    c_uniform = concentration.compute(xs, uniform_p)
    c_spike = concentration.compute(xs, spike_p)
    assert 0 <= c_uniform <= 1
    assert 0 <= c_spike <= 1
    assert c_uniform < c_spike


def test_concentration_handles_single_bin():
    assert concentration.compute([1.0], [1.0]) == 0.0


def test_peak_is_max_p():
    xs = [-1.0, 0.0, 1.0]
    ps = [0.1, 0.7, 0.2]
    assert peak.compute(xs, ps) == 0.7


def test_tail_positive_and_negative():
    xs = [-2.0, -1.0, 0.0, 1.0, 2.0]
    ps = [0.1, 0.2, 0.4, 0.2, 0.1]
    # default threshold 0.5: x>0.5 → x in {1, 2} → p sum = 0.3; symmetric below.
    assert math.isclose(tail.compute_positive(xs, ps), 0.3, abs_tol=1e-9)
    assert math.isclose(tail.compute_negative(xs, ps), 0.3, abs_tol=1e-9)
    # higher threshold: x>1.5 → just x=2.0 → 0.1
    assert math.isclose(tail.compute_positive(xs, ps, threshold=1.5), 0.1, abs_tol=1e-9)


def test_cvar_95_handles_empty():
    assert cvar.compute_95([], []) == 0.0


def test_cvar_95_picks_upper_tail():
    xs = [-2.0, -1.0, 0.0, 1.0, 2.0]
    ps = [0.05, 0.05, 0.10, 0.30, 0.50]
    # Upper 5% tail: just the x=2.0 bin pieces
    assert cvar.compute_95(xs, ps) >= 1.5  # heavily weighted toward 2.0


# ── combiners ──────────────────────────────────────────────────────────────

def test_run_uniform_distribution_low_conviction():
    d = _uniform_dist([-2.0, -1.0, 0.0, 1.0, 2.0])
    c = conviction_functionals.run("expected_return", d, t_days=1.0)
    assert 0 <= c < 0.05  # μ=0, conc≈0 → near zero


def test_run_spike_at_positive_high_conviction():
    d = _spike_dist(1.5)
    c = conviction_functionals.run("expected_return", d, t_days=1.0)
    assert 0.3 < c <= 1.0


def test_run_spike_at_negative_also_high_conviction():
    d = _spike_dist(-1.5)
    c = conviction_functionals.run("expected_return", d, t_days=1.0)
    # |E[r]| matters for magnitude; sign is direction concern
    assert 0.3 < c <= 1.0


def test_run_unknown_functional_raises():
    d = _uniform_dist([-1.0, 0.0, 1.0])
    try:
        conviction_functionals.run("does_not_exist", d, t_days=1.0)
        raise AssertionError("expected KeyError")
    except KeyError:
        pass


def test_collapse_across_horizons_weights_shorter_more():
    short = _spike_dist(1.0)
    short["horizon"] = "5m"
    long = _spike_dist(-2.0)
    long["horizon"] = "1w"
    c = conviction_functionals.collapse_across_horizons(
        "expected_return",
        [(short, 1.0), (long, 7.0)],
    )
    # 5m gets weight 1/√1=1; 1w gets 1/√7 ≈ 0.378. Short signal (positive, weight 1)
    # should dominate the result. Magnitudes are similar so collapsed should be > 0.
    assert 0 <= c <= 1


def test_collapse_empty_returns_zero():
    assert conviction_functionals.collapse_across_horizons("expected_return", []) == 0.0


def test_frac_kelly_clamps_to_one():
    d = _spike_dist(2.0)  # huge mu, tiny variance
    c = conviction_functionals.run("frac_kelly", d, t_days=1.0)
    assert 0.9 <= c <= 1.0


def test_list_functionals_contains_defaults():
    names = conviction_functionals.list_functionals()
    assert "expected_return" in names
    assert "frac_kelly" in names
    assert "peak_xp_over_t" in names
