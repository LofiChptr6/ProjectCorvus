"""Unit tests for the resolver's pure helpers — intraday-pair lookup and
distribution scoring application. Resolver end-to-end paths that touch
Postgres + Massive are covered by the live smoke (not pytest)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import importlib


def _resolver():
    return importlib.import_module("scripts.run_forecast_resolver")


def _bar(ts: datetime, close: float) -> dict:
    return {"bar_time": ts, "c": close}


def test_intraday_pair_picks_first_bar_at_or_after_target():
    R = _resolver()
    base = datetime(2026, 5, 16, 14, 30, tzinfo=timezone.utc)
    bars = [_bar(base + timedelta(minutes=k * 5), 100.0 + k) for k in range(20)]
    pair = R._intraday_pair(bars, submitted_at=base, horizon_minutes=15)
    assert pair is not None
    entry, exit_ = pair
    assert entry == 100.0           # first bar at submitted_at
    assert exit_ == 103.0           # first bar at +15min (k=3)


def test_intraday_pair_returns_none_when_no_exit_bar():
    R = _resolver()
    base = datetime(2026, 5, 16, 14, 30, tzinfo=timezone.utc)
    # Only one bar — no exit window available.
    bars = [_bar(base, 100.0)]
    assert R._intraday_pair(bars, submitted_at=base, horizon_minutes=60) is None


def test_intraday_pair_handles_empty_bars():
    R = _resolver()
    base = datetime(2026, 5, 16, 14, 30, tzinfo=timezone.utc)
    assert R._intraday_pair([], submitted_at=base, horizon_minutes=5) is None


def test_apply_scoring_parses_json_string():
    R = _resolver()
    import json
    distribution = {
        "anchor_price": 100.0,
        "anchor_ts": "2026-05-16T17:30:00+00:00",
        "axis": "return_pct",
        "horizon": "5m",
        "bins": [
            {"x": -2.0, "p": 0.05},
            {"x": -1.0, "p": 0.20},
            {"x":  0.0, "p": 0.50},
            {"x":  1.0, "p": 0.20},
            {"x":  2.0, "p": 0.05},
        ],
        "model": "test",
        "model_version": "0",
    }
    out = R._apply_scoring(json.dumps(distribution), realized_pct=0.3)
    assert out is not None
    assert set(out.keys()) >= {"log_loss", "brier", "crps", "pinball05", "pinball95",
                               "realized_bin_idx"}
    assert out["realized_bin_idx"] == 2


def test_apply_scoring_returns_none_on_missing_distribution():
    R = _resolver()
    assert R._apply_scoring(None, realized_pct=0.0) is None
    assert R._apply_scoring({"bins": []}, realized_pct=0.0) is None
    assert R._apply_scoring("not json {", realized_pct=0.0) is None
