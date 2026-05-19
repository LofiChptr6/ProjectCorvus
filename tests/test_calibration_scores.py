"""Calibration-scoring unit tests. Covers log-loss, Brier, CRPS via empirical
step-CDF, and pinball loss at the 5/95th percentiles."""
from __future__ import annotations

import math

import pytest

from meta_agent.calibration_scores import (
    brier,
    crps_step_cdf,
    log_loss,
    pinball,
    realized_bin_index,
    score_distribution,
)


def _uniform_xs(n: int = 5, lo: float = -2.0, hi: float = 2.0) -> list[float]:
    spacing = (hi - lo) / (n - 1)
    return [lo + i * spacing for i in range(n)]


# ── realized_bin_index ───────────────────────────────────────────────────────

def test_realized_bin_centered_on_zero():
    xs = _uniform_xs(5, -2.0, 2.0)  # centers at -2, -1, 0, 1, 2; spacing 1
    assert realized_bin_index(xs, 0.0) == 2
    assert realized_bin_index(xs, -1.0) == 1
    assert realized_bin_index(xs, 1.5) == 4   # right edge of bin 3 is 1.5; goes to bin 4
    assert realized_bin_index(xs, 0.49) == 2  # still within bin 2's right edge (0.5)


def test_realized_bin_clips_left():
    xs = _uniform_xs(5)
    assert realized_bin_index(xs, -100.0) == 0


def test_realized_bin_clips_right():
    xs = _uniform_xs(5)
    assert realized_bin_index(xs, 100.0) == 4


# ── log_loss ────────────────────────────────────────────────────────────────

def test_log_loss_perfect_assignment():
    ps = [0.001, 0.001, 0.997, 0.001]
    assert log_loss(ps, 2) == pytest.approx(-math.log(0.997), abs=1e-9)


def test_log_loss_zero_mass_bin_uses_floor():
    # log_loss internally clamps p to 1e-12 so this is finite, not -inf
    ps = [0.5, 0.5, 0.0]
    assert log_loss(ps, 2) > 20  # very large but finite


# ── brier ──────────────────────────────────────────────────────────────────

def test_brier_uniform_5bin_predicted():
    ps = [0.2] * 5
    # (0.2 - 0)^2 × 4 + (0.2 - 1)^2 = 4*0.04 + 0.64 = 0.8
    assert brier(ps, 2) == pytest.approx(0.8, abs=1e-9)


def test_brier_spike_at_realized_is_minimum():
    spike = [0.001, 0.001, 0.997, 0.001]
    uniform = [0.25] * 4
    assert brier(spike, 2) < brier(uniform, 2)


# ── crps_step_cdf ──────────────────────────────────────────────────────────

def test_crps_zero_when_realized_at_spike_center():
    xs = _uniform_xs(5, -2.0, 2.0)  # spacing 1.0
    ps = [1e-4, 1e-4, 1 - 4e-4, 1e-4, 1e-4]
    crps = crps_step_cdf(xs, ps, 0.0)
    # Tiny but non-zero because CDF is a step function not Dirac. Expect
    # very small relative to spacing.
    assert 0 <= crps < 0.5


def test_crps_grows_with_distance_from_realized():
    xs = _uniform_xs(5, -2.0, 2.0)
    ps = [0.2] * 5
    c_at_zero = crps_step_cdf(xs, ps, 0.0)
    c_at_two = crps_step_cdf(xs, ps, 2.0)
    c_outside = crps_step_cdf(xs, ps, 5.0)
    assert c_at_two > c_at_zero  # realization on tail edge worse than on mode
    assert c_outside > c_at_two  # out-of-support gap dominates


# ── pinball ────────────────────────────────────────────────────────────────

def test_pinball_zero_when_realized_equals_quantile():
    # Symmetric distribution; 0.5-quantile = 0; pinball(0.5, 0) at y=0 → 0
    xs = _uniform_xs(5, -2.0, 2.0)
    ps = [0.2] * 5
    # For α=0.5, q ≈ 0; at realized=0, indicator = 0, loss = 0.5 * 0 = 0
    assert abs(pinball(xs, ps, 0.0, 0.5)) < 1e-9


def test_pinball_penalizes_underprediction_at_high_alpha():
    # Distribution centered low; realized blows past q95 → big underprediction
    xs = _uniform_xs(5, -2.0, 2.0)
    ps = [0.4, 0.3, 0.2, 0.07, 0.03]
    loss_at_realized_3 = pinball(xs, ps, 3.0, 0.95)
    loss_at_realized_0 = pinball(xs, ps, 0.0, 0.95)
    assert loss_at_realized_3 > loss_at_realized_0


# ── score_distribution end-to-end ──────────────────────────────────────────

def test_score_distribution_returns_all_keys():
    d = {
        "bins": [
            {"x": -2.0, "p": 0.05},
            {"x": -1.0, "p": 0.20},
            {"x":  0.0, "p": 0.50},
            {"x":  1.0, "p": 0.20},
            {"x":  2.0, "p": 0.05},
        ],
    }
    out = score_distribution(d, realized=0.3)
    assert set(out.keys()) == {
        "realized_bin_idx", "log_loss", "brier", "crps", "pinball05", "pinball95",
    }
    assert out["realized_bin_idx"] == 2
    assert out["log_loss"] > 0
    assert 0 <= out["brier"] <= 2
    assert out["crps"] >= 0


def test_score_distribution_empty_bins():
    assert score_distribution({"bins": []}, realized=0.0) == {}
