"""Tests for the per-(symbol, rule) OCAP cooldown.

The cooldown sits between rule evaluation and job enqueue — same rule on
same symbol can't re-fire within `ocap_cooldown_s` (default 30 min). Soft
in-process suppression that resets on streamer restart.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from analysis import indicators_ocap


@pytest.fixture(autouse=True)
def _reset_cooldown_map():
    indicators_ocap._FIRED_AT.clear()
    yield
    indicators_ocap._FIRED_AT.clear()


def test_first_fire_passes_through():
    out = indicators_ocap._apply_cooldown("SPY", ["rsi_extreme"], cooldown_s=60)
    assert out == ["rsi_extreme"]


def test_second_fire_within_window_suppressed():
    indicators_ocap._apply_cooldown("SPY", ["rsi_extreme"], cooldown_s=60)
    out = indicators_ocap._apply_cooldown("SPY", ["rsi_extreme"], cooldown_s=60)
    assert out == []


def test_second_fire_after_window_passes():
    # Seed cooldown at t = now - 100s (older than 60s window).
    indicators_ocap._FIRED_AT[("SPY", "rsi_extreme")] = time.time() - 100
    out = indicators_ocap._apply_cooldown("SPY", ["rsi_extreme"], cooldown_s=60)
    assert out == ["rsi_extreme"]


def test_different_rules_independent():
    indicators_ocap._apply_cooldown("SPY", ["rsi_extreme"], cooldown_s=60)
    out = indicators_ocap._apply_cooldown("SPY", ["bollinger_break"], cooldown_s=60)
    assert out == ["bollinger_break"]


def test_different_symbols_independent():
    indicators_ocap._apply_cooldown("SPY", ["rsi_extreme"], cooldown_s=60)
    out = indicators_ocap._apply_cooldown("QQQ", ["rsi_extreme"], cooldown_s=60)
    assert out == ["rsi_extreme"]


def test_disabled_when_cooldown_is_zero():
    indicators_ocap._apply_cooldown("SPY", ["rsi_extreme"], cooldown_s=0)
    out = indicators_ocap._apply_cooldown("SPY", ["rsi_extreme"], cooldown_s=0)
    assert out == ["rsi_extreme"]


def test_symbol_case_insensitive():
    indicators_ocap._apply_cooldown("spy", ["rsi_extreme"], cooldown_s=60)
    out = indicators_ocap._apply_cooldown("SPY", ["rsi_extreme"], cooldown_s=60)
    assert out == []


def test_multi_rule_partial_suppression():
    """Only the rule that fired recently gets suppressed; the other passes."""
    indicators_ocap._apply_cooldown("SPY", ["rsi_extreme"], cooldown_s=60)
    out = indicators_ocap._apply_cooldown(
        "SPY", ["rsi_extreme", "bollinger_break"], cooldown_s=60,
    )
    assert out == ["bollinger_break"]


def test_anchor_resets_on_each_survived_fire():
    """A surviving fire updates the last-fired timestamp so subsequent fires
    measure from THIS one, not the original (cooldown rolls forward)."""
    # Seed at t-100 (outside 60s window) → passes.
    indicators_ocap._FIRED_AT[("SPY", "rsi_extreme")] = time.time() - 100
    out1 = indicators_ocap._apply_cooldown("SPY", ["rsi_extreme"], cooldown_s=60)
    assert out1 == ["rsi_extreme"]
    # Immediately again — should be suppressed because we just refreshed.
    out2 = indicators_ocap._apply_cooldown("SPY", ["rsi_extreme"], cooldown_s=60)
    assert out2 == []
