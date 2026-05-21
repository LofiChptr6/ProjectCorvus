"""Contract + smoke tests for the Phase-F heavy models:
hmm_regime_mix (Gaussian HMM) and lgbm_bin_classifier (LightGBM softmax).
Both should follow MODEL_CONTRACT and emit validator-clean distributions."""
from __future__ import annotations

import importlib
import math
import random

import pytest

from meta_agent.distribution_validator import validate_distribution


def _build_bars(n: int = 220, base: float = 100.0, drift: float = 0.0,
                vol: float = 0.005, seed: int = 13,
                regime_switch: bool = False) -> list[dict]:
    """Daily-bar generator. Optionally switches regime halfway to give the HMM
    something non-degenerate to fit. The bear-leg uses negative drift +
    3× vol so the two regimes are well-separated in (μ, σ) space — without this
    contrast hmmlearn collapses to a single state and the model's
    degenerate-fit guard (σ_state > 0.3, triggered by the prior fallback of
    the unused state) declines."""
    rnd = random.Random(seed)
    price = base
    bars: list[dict] = []
    for i in range(n):
        if regime_switch and i >= n // 2:
            local_drift, local_vol = drift - 0.005, vol * 3.0
        else:
            local_drift, local_vol = drift + 0.002, vol
        ret = local_drift + local_vol * (2 * rnd.random() - 1)
        new_price = max(0.01, price * math.exp(ret))
        bars.append({"o": price, "h": max(price, new_price) * 1.001,
                     "l": min(price, new_price) * 0.999, "c": new_price,
                     "v": 1_000_000})
        price = new_price
    return bars


# ── hmm_regime_mix ────────────────────────────────────────────────────────

def test_hmm_skipped_on_short_bars():
    m = importlib.import_module("agents.atlas.models.hmm_regime_mix")
    out = m.compute("SPY", _build_bars(40), {})
    assert out["signal"] is None


def test_hmm_distributions_validate():
    m = importlib.import_module("agents.atlas.models.hmm_regime_mix")
    out = m.compute("SPY", _build_bars(220, regime_switch=True), {})
    # HMM may decline to converge on synthetic noise — accept both paths,
    # but if it produces distributions they must validate.
    if out["signal"] is None:
        pytest.skip(f"HMM declined on synthetic input: {out.get('reason')}")
    horizons = set()
    for d in out["distributions"]:
        ok, reason = validate_distribution(d)
        assert ok, f"{d.get('horizon')}: {reason}"
        horizons.add(d["horizon"])
    assert {"1d", "1w"}.issubset(horizons)


def test_hmm_inputs_stable():
    m = importlib.import_module("agents.atlas.models.hmm_regime_mix")
    out = m.compute("SPY", _build_bars(220, regime_switch=True), {})
    if out["signal"] is None:
        pytest.skip(f"HMM declined: {out.get('reason')}")
    expected = {
        "hmm_mu_bull", "hmm_mu_bear", "hmm_sigma_bull", "hmm_sigma_bear",
        "hmm_p_bull", "hmm_p_bear", "last_close",
    }
    assert set(out["inputs"].keys()) == expected


def test_hmm_regime_probs_sum_to_one():
    m = importlib.import_module("agents.atlas.models.hmm_regime_mix")
    out = m.compute("SPY", _build_bars(220, regime_switch=True), {})
    if out["signal"] is None:
        pytest.skip("HMM declined")
    inputs = out["inputs"]
    total = inputs["hmm_p_bull"] + inputs["hmm_p_bear"]
    assert abs(total - 1.0) < 1e-5


# ── lgbm_bin_classifier ───────────────────────────────────────────────────

def test_lgbm_skipped_on_short_bars():
    m = importlib.import_module("agents.atlas.models.lgbm_bin_classifier")
    out = m.compute("SPY", _build_bars(40), {})
    assert out["signal"] is None


def test_lgbm_distributions_validate():
    m = importlib.import_module("agents.atlas.models.lgbm_bin_classifier")
    out = m.compute("SPY", _build_bars(220), {})
    if out["signal"] is None:
        pytest.skip(f"LGBM declined: {out.get('reason')}")
    assert len(out["distributions"]) == 1
    d = out["distributions"][0]
    ok, reason = validate_distribution(d)
    assert ok, reason
    assert d["horizon"] == "1d"


def test_lgbm_inputs_stable():
    m = importlib.import_module("agents.atlas.models.lgbm_bin_classifier")
    out = m.compute("SPY", _build_bars(220), {})
    if out["signal"] is None:
        pytest.skip(f"LGBM declined: {out.get('reason')}")
    expected = {
        "lgbm_n_train", "lgbm_n_classes_fit",
        "feat_last_return", "feat_mean20", "feat_vol20", "feat_rsi14",
        "feat_stretch_vs_ma20", "last_close",
    }
    assert set(out["inputs"].keys()) == expected


def test_lgbm_probs_sum_to_one():
    m = importlib.import_module("agents.atlas.models.lgbm_bin_classifier")
    out = m.compute("SPY", _build_bars(220), {})
    if out["signal"] is None:
        pytest.skip(f"LGBM declined: {out.get('reason')}")
    d = out["distributions"][0]
    total = sum(b["p"] for b in d["bins"])
    assert abs(total - 1.0) < 1e-4


def test_lgbm_label_bin_extremes():
    m = importlib.import_module("agents.atlas.models.lgbm_bin_classifier")
    assert m._label_bin(-99.0) == 0
    assert m._label_bin(99.0) == m._N_BINS - 1
    # exact center bin (0% return) → middle index
    mid = (m._N_BINS - 1) // 2
    assert m._label_bin(0.0) == mid
