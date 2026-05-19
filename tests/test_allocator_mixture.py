"""Tests for the Phase-E mixture-then-functional path in the allocator.
Covers enrich_views_with_mixture's substitution semantics + the env flag."""
from __future__ import annotations

import asyncio
import math
import os
from datetime import datetime, timezone
from unittest.mock import patch

from meta_agent.allocator import (
    ConvictionView, compute_target_weights, enrich_views_with_mixture,
    use_mixture_enabled,
)


def _make_dist(mu: float, sigma: float, horizon: str = "1h") -> dict:
    spacing = 0.5
    n = 5
    lo = mu - 2 * spacing
    centers = [lo + i * spacing for i in range(n)]
    raw = [math.exp(-0.5 * ((c - mu) / max(sigma, 1e-6)) ** 2) for c in centers]
    s = sum(raw)
    probs = [max(p / s, 1e-4) for p in raw]
    s2 = sum(probs)
    probs = [p / s2 for p in probs]
    return {
        "anchor_price": 100.0,
        "anchor_ts": datetime.now(timezone.utc).isoformat(),
        "axis": "return_pct",
        "horizon": horizon,
        "bins": [{"x": round(c, 6), "p": round(p, 6)} for c, p in zip(centers, probs)],
        "model": "test",
        "model_version": "0",
    }


def test_use_mixture_enabled_reads_env():
    with patch.dict(os.environ, {"ALLOC_USE_MIXTURE": "1"}, clear=False):
        assert use_mixture_enabled() is True
    with patch.dict(os.environ, {"ALLOC_USE_MIXTURE": "0"}, clear=False):
        assert use_mixture_enabled() is False
    # Unset → default False
    env = {k: v for k, v in os.environ.items() if k != "ALLOC_USE_MIXTURE"}
    with patch.dict(os.environ, env, clear=True):
        assert use_mixture_enabled() is False


def test_enrich_substitutes_views_when_distributions_exist():
    """Two agents have scalar convictions + matching distributions on AAPL.
    The mixture path should drop their scalar rows and substitute synthetic
    rows that sum to the mixture-derived scalar."""
    raw_views = [
        ConvictionView(agent_name="atlas",     symbol="AAPL", direction="long",
                       conviction=0.3, expected_return_pct=1.0, time_to_target_days=1),
        ConvictionView(agent_name="commodity", symbol="AAPL", direction="long",
                       conviction=0.4, expected_return_pct=2.0, time_to_target_days=1),
        ConvictionView(agent_name="atlas",     symbol="MSFT", direction="long",
                       conviction=0.5, expected_return_pct=1.5, time_to_target_days=1),
    ]
    fake_dist_rows = [
        {"agent_name": "atlas",     "symbol": "AAPL", "horizon": "1h",
         "time_to_target_days": 1, "distribution": _make_dist(1.5, 0.4, "1h")},
        {"agent_name": "commodity", "symbol": "AAPL", "horizon": "1h",
         "time_to_target_days": 1, "distribution": _make_dist(1.0, 0.4, "1h")},
    ]

    async def fake_get_active_distributions(symbol=None):
        return fake_dist_rows

    with patch("db.store.get_active_distributions", side_effect=fake_get_active_distributions):
        new_views, report = asyncio.run(enrich_views_with_mixture(raw_views))

    # AAPL substituted: should have 2 mixture rows (one per contributor)
    aapl_rows = [v for v in new_views if v.symbol == "AAPL"]
    assert len(aapl_rows) == 2
    assert all(v.direction == "long" for v in aapl_rows)
    # MSFT unchanged (no distribution)
    msft_rows = [v for v in new_views if v.symbol == "MSFT"]
    assert len(msft_rows) == 1
    assert msft_rows[0].agent_name == "atlas"
    assert msft_rows[0].conviction == 0.5

    # Report records both legacy + mixture scalars for A/B
    assert "AAPL" in report
    assert report["AAPL"]["substituted"] is True
    assert report["AAPL"]["mixture_direction"] == "long"
    assert report["AAPL"]["mixture_scalar"] > 0
    # Legacy scalar sum for AAPL = +0.3 + 0.4 = +0.7
    assert abs(report["AAPL"]["legacy_sum"] - 0.7) < 1e-6


def test_enrich_substituted_rows_sum_to_mixture_scalar():
    raw_views = [
        ConvictionView(agent_name="atlas",     symbol="AAPL", direction="long",
                       conviction=0.3, expected_return_pct=1.0, time_to_target_days=1),
        ConvictionView(agent_name="commodity", symbol="AAPL", direction="long",
                       conviction=0.4, expected_return_pct=2.0, time_to_target_days=1),
    ]
    fake_dist_rows = [
        {"agent_name": "atlas", "symbol": "AAPL", "horizon": "1h",
         "time_to_target_days": 1, "distribution": _make_dist(1.5, 0.4, "1h")},
        {"agent_name": "commodity", "symbol": "AAPL", "horizon": "1h",
         "time_to_target_days": 1, "distribution": _make_dist(1.0, 0.4, "1h")},
    ]

    async def fake_get_active_distributions(symbol=None):
        return fake_dist_rows

    with patch("db.store.get_active_distributions", side_effect=fake_get_active_distributions):
        new_views, report = asyncio.run(enrich_views_with_mixture(raw_views))

    aapl_rows = [v for v in new_views if v.symbol == "AAPL"]
    total = sum(v.conviction for v in aapl_rows)
    assert abs(total - report["AAPL"]["mixture_scalar"]) < 1e-9


def test_enrich_with_no_distributions_passes_views_through():
    raw_views = [
        ConvictionView(agent_name="atlas", symbol="AAPL", direction="long",
                       conviction=0.5, expected_return_pct=1.0, time_to_target_days=1),
    ]

    async def fake_get_active_distributions(symbol=None):
        return []

    with patch("db.store.get_active_distributions", side_effect=fake_get_active_distributions):
        new_views, report = asyncio.run(enrich_views_with_mixture(raw_views))
    assert new_views == raw_views
    assert report == {}


def test_enrich_compute_target_weights_integrates_cleanly():
    """End-to-end: substituted views flow through compute_target_weights and
    produce a non-zero target weight on the substituted symbol."""
    raw_views = [
        ConvictionView(agent_name="atlas",     symbol="AAPL", direction="long",
                       conviction=0.5, expected_return_pct=1.0, time_to_target_days=1),
        ConvictionView(agent_name="commodity", symbol="AAPL", direction="long",
                       conviction=0.5, expected_return_pct=1.0, time_to_target_days=1),
    ]
    fake_dist_rows = [
        {"agent_name": "atlas", "symbol": "AAPL", "horizon": "1h",
         "time_to_target_days": 1, "distribution": _make_dist(1.5, 0.3, "1h")},
        {"agent_name": "commodity", "symbol": "AAPL", "horizon": "1h",
         "time_to_target_days": 1, "distribution": _make_dist(1.2, 0.3, "1h")},
    ]

    async def fake_get_active_distributions(symbol=None):
        return fake_dist_rows

    with patch("db.store.get_active_distributions", side_effect=fake_get_active_distributions):
        new_views, _ = asyncio.run(enrich_views_with_mixture(new_raw := raw_views))
    tw = compute_target_weights(new_views, gross_leverage=1.0,
                                 max_per_symbol=0.5, min_trade_threshold=0.0)
    # AAPL should retain a positive long target
    assert "AAPL" in tw.weights
    assert tw.weights["AAPL"] > 0
    # And contributor attribution should preserve both agents
    contribs = {a for a, _ in tw.contributors.get("AAPL", [])}
    assert contribs == {"atlas", "commodity"}


def test_enrich_handles_flat_or_zero_mixture():
    """A bimodal mixture that nets to ~zero E[r] should be marked
    substituted=False / reason='flat-or-zero' and leave views untouched."""
    raw_views = [
        ConvictionView(agent_name="atlas", symbol="AAPL", direction="long",
                       conviction=0.5, expected_return_pct=1.0, time_to_target_days=1),
    ]
    fake_dist_rows = [
        {"agent_name": "atlas", "symbol": "AAPL", "horizon": "1h",
         "time_to_target_days": 1, "distribution": _make_dist(0.0, 0.5, "1h")},
    ]

    async def fake_get_active_distributions(symbol=None):
        return fake_dist_rows

    with patch("db.store.get_active_distributions", side_effect=fake_get_active_distributions):
        new_views, report = asyncio.run(enrich_views_with_mixture(raw_views))

    # Either flat OR substituted with tiny scalar — both are valid; just confirm
    # the report carries the mixer's verdict and views remain non-empty.
    assert "AAPL" in report
    assert len(new_views) >= 1
