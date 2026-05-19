"""Smoke + contract test for atlas/ou_mean_revert. Verifies the model
follows MODEL_CONTRACT.md and emits validator-clean distributions."""
from __future__ import annotations

import importlib
import math
import random

from meta_agent.distribution_validator import validate_distribution


def _build_bars(n: int = 60, base: float = 100.0, drift: float = 0.0,
                vol: float = 0.005, seed: int = 7) -> list[dict]:
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


def test_skipped_on_short_bars():
    m = importlib.import_module("agents.atlas.models.ou_mean_revert")
    out = m.compute("AAPL", _build_bars(10), {})
    assert out["signal"] is None
    assert out["direction"] is None


def test_returns_validator_clean_distributions():
    m = importlib.import_module("agents.atlas.models.ou_mean_revert")
    out = m.compute("AAPL", _build_bars(60), {})
    assert "distributions" in out
    dists = out["distributions"]
    assert len(dists) >= 2
    horizons_seen = set()
    for d in dists:
        ok, reason = validate_distribution(d)
        assert ok, f"{d.get('horizon')}: {reason}"
        horizons_seen.add(d["horizon"])
    assert {"5m", "1h"}.issubset(horizons_seen)


def test_inputs_stable_keys():
    """Model_inputs_validator pins the set of `inputs` keys per agent. New
    versions must keep stable keys so the registry doesn't reject submissions."""
    m = importlib.import_module("agents.atlas.models.ou_mean_revert")
    out = m.compute("AAPL", _build_bars(60), {})
    inputs = out.get("inputs") or {}
    expected = {"ar1_alpha", "ar1_beta", "sigma_resid", "last_log_return", "last_close"}
    assert set(inputs.keys()) == expected


def test_direction_matches_sign_of_expected_return():
    m = importlib.import_module("agents.atlas.models.ou_mean_revert")
    # Use a positive-drift sequence to make E[r] more likely positive
    out = m.compute("AAPL", _build_bars(60, drift=0.001), {})
    if out["direction"] in ("long", "short"):
        assert math.copysign(1, out["expected_return_pct"]) == (1 if out["direction"] == "long" else -1)
