"""Tests for meta_agent.conviction_diff — the materiality gate.

The gate decides whether a completed OCAP review should fire an
ocap_rebalance. False negatives (call something material when it isn't)
waste an allocator run; false positives (call something immaterial when
it isn't) skip a needed rebalance. The defaults are conservative — when
in doubt, mark material. These tests pin the conservative behaviour.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from meta_agent.conviction_diff import (
    is_material,
    count_material_changes,
    CONVICTION_DELTA_THRESHOLD,
    ER_DELTA_THRESHOLD_PP,
)


# ── is_material ────────────────────────────────────────────────────────────────


def test_no_prior_is_material():
    material, reason = is_material(None, {
        "direction": "long", "conviction": 0.5, "expected_return_pct": 2.0,
    })
    assert material is True
    assert reason == "new"


def test_identical_row_is_not_material():
    prior = {"direction": "long", "conviction": 0.50,
             "expected_return_pct": 2.0, "stop_pct": None}
    new = {"direction": "long", "conviction": 0.50,
           "expected_return_pct": 2.0, "stop_pct": None}
    material, reason = is_material(prior, new)
    assert material is False
    assert reason == "no_material_change"


def test_direction_flip_is_material():
    prior = {"direction": "long", "conviction": 0.5, "expected_return_pct": 2.0}
    new = {"direction": "flat", "conviction": 0.0, "expected_return_pct": 0.0}
    material, reason = is_material(prior, new)
    assert material is True
    assert "long" in reason and "flat" in reason


def test_conviction_delta_above_threshold_is_material():
    prior = {"direction": "long", "conviction": 0.40, "expected_return_pct": 2.0}
    new = {"direction": "long", "conviction": 0.50, "expected_return_pct": 2.0}
    # delta = 0.10 > 0.05 threshold
    material, reason = is_material(prior, new)
    assert material is True
    assert "conv" in reason


def test_conviction_delta_below_threshold_is_not_material():
    prior = {"direction": "long", "conviction": 0.50, "expected_return_pct": 2.0}
    new = {"direction": "long", "conviction": 0.52, "expected_return_pct": 2.0}
    # delta = 0.02 < 0.05 threshold
    material, reason = is_material(prior, new)
    assert material is False


def test_conviction_delta_exact_threshold_is_material():
    """Exactly-at threshold counts as material — conservative bias."""
    prior = {"direction": "long", "conviction": 0.50, "expected_return_pct": 2.0}
    new = {"direction": "long", "conviction": 0.50 + CONVICTION_DELTA_THRESHOLD,
           "expected_return_pct": 2.0}
    material, _ = is_material(prior, new)
    assert material is True


def test_er_delta_above_threshold_is_material():
    prior = {"direction": "long", "conviction": 0.50, "expected_return_pct": 2.0}
    new = {"direction": "long", "conviction": 0.50, "expected_return_pct": 4.0}
    # delta = 2pp > 1pp threshold
    material, reason = is_material(prior, new)
    assert material is True
    assert "er" in reason


def test_er_delta_below_threshold_is_not_material():
    prior = {"direction": "long", "conviction": 0.50, "expected_return_pct": 2.0}
    new = {"direction": "long", "conviction": 0.50, "expected_return_pct": 2.5}
    material, _ = is_material(prior, new)
    assert material is False


def test_er_presence_flip_is_material():
    """None → number (or vice versa) means the agent added/dropped a forecast."""
    prior = {"direction": "long", "conviction": 0.5, "expected_return_pct": None}
    new = {"direction": "long", "conviction": 0.5, "expected_return_pct": 2.0}
    material, reason = is_material(prior, new)
    assert material is True
    assert "er_presence" in reason


def test_stop_pct_presence_flip_is_material():
    """Adding or removing a stop is always material."""
    prior = {"direction": "long", "conviction": 0.5, "expected_return_pct": 2.0, "stop_pct": None}
    new = {"direction": "long", "conviction": 0.5, "expected_return_pct": 2.0, "stop_pct": 5.0}
    material, reason = is_material(prior, new)
    assert material is True
    assert "stop_presence" in reason


def test_stop_pct_magnitude_change_alone_is_not_material():
    """Adjusting a stop within 'with-stop' is not by itself material — the
    allocator only cares at the band edge."""
    prior = {"direction": "long", "conviction": 0.5, "expected_return_pct": 2.0, "stop_pct": 5.0}
    new = {"direction": "long", "conviction": 0.5, "expected_return_pct": 2.0, "stop_pct": 6.0}
    material, _ = is_material(prior, new)
    assert material is False


# ── count_material_changes ─────────────────────────────────────────────────────


def test_count_zero_when_all_identical():
    prior = [
        {"symbol": "SPY", "direction": "long", "conviction": 0.5, "expected_return_pct": 2.0, "stop_pct": None},
        {"symbol": "QQQ", "direction": "long", "conviction": 0.4, "expected_return_pct": 1.5, "stop_pct": None},
    ]
    new = [
        {"symbol": "SPY", "direction": "long", "conviction": 0.5, "expected_return_pct": 2.0, "stop_pct": None},
        {"symbol": "QQQ", "direction": "long", "conviction": 0.4, "expected_return_pct": 1.5, "stop_pct": None},
    ]
    n, reasons = count_material_changes(prior, new)
    assert n == 0
    assert reasons == []


def test_count_one_when_single_conviction_shifts():
    prior = [
        {"symbol": "SPY", "direction": "long", "conviction": 0.4, "expected_return_pct": 2.0, "stop_pct": None},
        {"symbol": "QQQ", "direction": "long", "conviction": 0.4, "expected_return_pct": 1.5, "stop_pct": None},
    ]
    new = [
        {"symbol": "SPY", "direction": "long", "conviction": 0.7, "expected_return_pct": 2.0, "stop_pct": None},
        {"symbol": "QQQ", "direction": "long", "conviction": 0.4, "expected_return_pct": 1.5, "stop_pct": None},
    ]
    n, reasons = count_material_changes(prior, new)
    assert n == 1
    assert any("SPY" in r for r in reasons)


def test_implicit_flat_is_material():
    """Symbol held before, omitted now → counts as material (agent backed off)."""
    prior = [
        {"symbol": "SPY", "direction": "long", "conviction": 0.4, "expected_return_pct": 2.0, "stop_pct": None},
        {"symbol": "QQQ", "direction": "long", "conviction": 0.4, "expected_return_pct": 1.5, "stop_pct": None},
    ]
    new = [
        {"symbol": "SPY", "direction": "long", "conviction": 0.4, "expected_return_pct": 2.0, "stop_pct": None},
        # QQQ dropped
    ]
    n, reasons = count_material_changes(prior, new)
    assert n == 1
    assert any("QQQ" in r and "implicit_flat" in r for r in reasons)


def test_new_symbol_is_material():
    """Fresh commitment on a new name → always material."""
    prior = [
        {"symbol": "SPY", "direction": "long", "conviction": 0.4, "expected_return_pct": 2.0, "stop_pct": None},
    ]
    new = [
        {"symbol": "SPY", "direction": "long", "conviction": 0.4, "expected_return_pct": 2.0, "stop_pct": None},
        {"symbol": "IWM", "direction": "long", "conviction": 0.3, "expected_return_pct": 1.2, "stop_pct": None},
    ]
    n, reasons = count_material_changes(prior, new)
    assert n == 1
    assert any("IWM" in r and r.endswith(":new") for r in reasons)


def test_empty_prior_all_material():
    """Fresh agent — every new conviction is material."""
    new = [
        {"symbol": "SPY", "direction": "long", "conviction": 0.4, "expected_return_pct": 2.0, "stop_pct": None},
        {"symbol": "QQQ", "direction": "long", "conviction": 0.4, "expected_return_pct": 1.5, "stop_pct": None},
    ]
    n, _ = count_material_changes([], new)
    assert n == 2


def test_symbol_case_insensitive_match():
    """Prior 'spy' vs new 'SPY' must compare as the same symbol."""
    prior = [
        {"symbol": "spy", "direction": "long", "conviction": 0.5, "expected_return_pct": 2.0, "stop_pct": None},
    ]
    new = [
        {"symbol": "SPY", "direction": "long", "conviction": 0.5, "expected_return_pct": 2.0, "stop_pct": None},
    ]
    n, _ = count_material_changes(prior, new)
    assert n == 0
