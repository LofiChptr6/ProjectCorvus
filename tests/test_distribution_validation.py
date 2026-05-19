"""Distribution-validator unit tests. Covers the contract enforced at
db.store.upsert_forecasts_batch and meta_agent.conviction_from_model."""
from __future__ import annotations

from meta_agent.distribution_validator import (
    MAX_BINS,
    MIN_BINS,
    MIN_P,
    SUM_TOLERANCE,
    horizon_to_minutes,
    horizon_to_ttd_days,
    validate_distribution,
)


def _good_distribution(horizon: str = "5m") -> dict:
    return {
        "anchor_price": 195.50,
        "anchor_ts": "2026-05-16T17:30:00+00:00",
        "axis": "return_pct",
        "horizon": horizon,
        "bins": [
            {"x": -2.0, "p": 0.05},
            {"x": -1.0, "p": 0.20},
            {"x":  0.0, "p": 0.50},
            {"x":  1.0, "p": 0.20},
            {"x":  2.0, "p": 0.05},
        ],
        "model": "ou_mean_revert",
        "model_version": "0.1.0",
    }


def test_validates_canonical_distribution():
    ok, reason = validate_distribution(_good_distribution())
    assert ok, reason


def test_rejects_non_dict():
    ok, reason = validate_distribution([1, 2, 3])
    assert not ok and "must be a dict" in reason


def test_rejects_missing_key():
    d = _good_distribution()
    del d["bins"]
    ok, reason = validate_distribution(d)
    assert not ok and "missing required key" in reason


def test_rejects_unknown_horizon():
    d = _good_distribution()
    d["horizon"] = "2d"
    ok, reason = validate_distribution(d)
    assert not ok and "horizon must be one of" in reason


def test_rejects_unknown_axis():
    d = _good_distribution()
    d["axis"] = "price"
    ok, reason = validate_distribution(d)
    assert not ok and "axis must be one of" in reason


def test_rejects_too_few_bins():
    d = _good_distribution()
    d["bins"] = d["bins"][:2]
    ok, reason = validate_distribution(d)
    assert not ok and f"[{MIN_BINS},{MAX_BINS}]" in reason


def test_rejects_too_many_bins():
    d = _good_distribution()
    n = MAX_BINS + 1
    spacing = 0.5
    raw = [{"x": -((n - 1) / 2) * spacing + i * spacing, "p": 1.0 / n} for i in range(n)]
    d["bins"] = raw
    ok, reason = validate_distribution(d)
    assert not ok and f"[{MIN_BINS},{MAX_BINS}]" in reason


def test_rejects_p_below_smoothing_floor():
    d = _good_distribution()
    d["bins"][0]["p"] = MIN_P / 10
    # renorm rest so sum stays ~1
    d["bins"][2]["p"] += MIN_P
    ok, reason = validate_distribution(d)
    assert not ok and "below smoothing floor" in reason


def test_rejects_non_monotonic_x():
    d = _good_distribution()
    d["bins"][2]["x"] = d["bins"][1]["x"]  # equal, not strictly increasing
    ok, reason = validate_distribution(d)
    assert not ok and "strictly increasing" in reason


def test_rejects_non_uniform_spacing():
    d = _good_distribution()
    d["bins"][2]["x"] = 0.5  # spacing 0.5, then 0.5, but jump 1.5 - 0.5 = 1.0 → non-uniform
    ok, reason = validate_distribution(d)
    assert not ok and "non-uniform spacing" in reason


def test_rejects_probabilities_not_summing_to_one():
    d = _good_distribution()
    d["bins"][0]["p"] = 0.5  # huge sum
    ok, reason = validate_distribution(d)
    assert not ok and "not within tolerance" in reason


def test_accepts_within_sum_tolerance():
    d = _good_distribution()
    # Bump first bin by 0.5 * tolerance (well inside the validator's slack)
    d["bins"][0]["p"] += SUM_TOLERANCE / 2
    ok, _ = validate_distribution(d)
    assert ok


def test_rejects_bad_anchor_ts():
    d = _good_distribution()
    d["anchor_ts"] = "not-a-timestamp"
    ok, reason = validate_distribution(d)
    assert not ok and "ISO-8601" in reason


def test_horizon_mappings():
    assert horizon_to_ttd_days("5m") == 1
    assert horizon_to_ttd_days("1h") == 1
    assert horizon_to_ttd_days("1d") == 1
    assert horizon_to_ttd_days("1w") == 7
    assert horizon_to_minutes("5m") == 5
    assert horizon_to_minutes("1h") == 60
    assert horizon_to_minutes("1d") == 60 * 24
    assert horizon_to_minutes("1w") == 60 * 24 * 7
