"""Unit tests for the replay-harness pure helpers — functional P&L derivation
and summary aggregation. End-to-end runs that touch Postgres are covered by
manual smoke (see `python scripts/replay_conviction_functional.py --help`)."""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone


def _replay():
    return importlib.import_module("scripts.replay_conviction_functional")


def _make_row(
    submitted_at: datetime,
    horizon: str = "1h",
    realized: float = 0.5,
    mu: float = 0.5,
    sigma: float = 0.4,
    agent: str = "atlas",
) -> dict:
    spacing = 0.5
    n = 5
    lo = mu - 2 * spacing
    centers = [lo + i * spacing for i in range(n)]
    import math
    raw = [math.exp(-0.5 * ((c - mu) / max(sigma, 1e-6)) ** 2) for c in centers]
    s = sum(raw)
    probs = [max(p / s, 1e-4) for p in raw]
    s2 = sum(probs)
    probs = [p / s2 for p in probs]
    dist = {
        "anchor_price": 100.0,
        "anchor_ts": submitted_at.isoformat(),
        "axis": "return_pct",
        "horizon": horizon,
        "bins": [{"x": round(c, 6), "p": round(p, 6)} for c, p in zip(centers, probs)],
        "model": "test",
        "model_version": "0",
    }
    return {
        "id": id(submitted_at),
        "agent_name": agent,
        "symbol": "AAPL",
        "horizon": horizon,
        "time_to_target_days": 1,
        "expected_return_pct": mu,
        "realized_return_pct": realized,
        "distribution": dist,
        "submitted_at": submitted_at,
        "resolved_at": submitted_at + timedelta(hours=1),
    }


def test_functional_pnls_positive_when_distribution_matches_realized():
    R = _replay()
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    rows = [_make_row(base + timedelta(days=k), mu=0.5, realized=0.4) for k in range(5)]
    pnls = R._functional_pnls(rows, "expected_return")
    assert all(p["pnl"] > 0 for p in pnls)


def test_functional_pnls_negative_when_distribution_opposite_realized():
    R = _replay()
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    rows = [_make_row(base + timedelta(days=k), mu=0.5, realized=-0.4) for k in range(5)]
    pnls = R._functional_pnls(rows, "expected_return")
    assert all(p["pnl"] < 0 for p in pnls)


def test_baseline_pnls_uses_legacy_scoring():
    R = _replay()
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    rows = [_make_row(base + timedelta(days=k), mu=0.5, realized=0.4) for k in range(5)]
    baseline = R._baseline_pnls(rows)
    assert len(baseline) == 5
    assert all(b["pnl"] > 0 for b in baseline)


def test_summarize_computes_sharpe_max_dd_winrate():
    R = _replay()
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    pnls = []
    # Alternating wins and losses, slight positive drift
    for k in range(10):
        pnl = 0.5 if k % 2 == 0 else -0.3
        pnls.append({
            "trade_date": (base + timedelta(days=k)).date(),
            "pnl": pnl, "scalar": 0.5, "sign": 1.0, "realized": pnl,
            "agent_name": "atlas", "horizon": "1h",
        })
    summary = R._summarize(pnls, label="test")
    assert summary["n"] == 10
    assert summary["n_days"] == 10
    assert summary["win_rate"] == 0.5
    assert summary["total_pnl"] > 0
    # max drawdown observed at -0.3 trough after each loss
    assert summary["max_drawdown"] >= 0.3


def test_summarize_empty_returns_n_zero():
    R = _replay()
    assert R._summarize([], label="empty") == {"label": "empty", "n": 0}


def test_resolve_window_supports_since_days():
    R = _replay()
    import argparse
    ns = argparse.Namespace(start=None, end=None, since_days=10)
    start, end = R._resolve_window(ns)
    from datetime import date
    assert end == date.today()
    assert (end - start).days == 10
