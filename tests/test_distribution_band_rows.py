"""Unit tests for `_distribution_band_rows` — the midpoint-edge math + clipping
+ row cap that translate stored agent_forecast distribution rows into the
Altair `mark_rect` band layer."""
from __future__ import annotations

from datetime import datetime, timezone

import importlib


def _dashboard():
    return importlib.import_module("obs.dashboard")


def _dist(anchor_price: float, xs: list[float], ps: list[float],
          submitted_at: datetime | None = None, horizon: str = "1h") -> dict:
    return {
        "submitted_at": submitted_at or datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc),
        "expires_at":   None,
        "horizon":      horizon,
        "time_to_target_days": 1,
        "distribution": {
            "anchor_price": anchor_price,
            "anchor_ts": (submitted_at or datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc)).isoformat(),
            "axis": "return_pct",
            "horizon": horizon,
            "bins": [{"x": x, "p": p} for x, p in zip(xs, ps)],
            "model": "test",
            "model_version": "0",
        },
    }


# ── Midpoint-edge rule (user spec) ────────────────────────────────────────

def test_three_bin_midpoint_edges_with_clip():
    """User spec from the dashboard message: y_centers = [99, 100, 102] should
    produce regions (-inf, 99.5], (99.5, 101], (101, +inf). With axis=return_pct
    the model uses y_center = anchor_price * (1 + x/100); to hit those y values
    use anchor_price=100 and xs=[-1, 0, 2] → y_centers = [99, 100, 102]."""
    D = _dashboard()
    d = _dist(anchor_price=100.0, xs=[-1.0, 0.0, 2.0],
              ps=[0.25, 0.5, 0.25])
    rows = D._distribution_band_rows([d], y_clip_lo=50.0, y_clip_hi=150.0)
    assert len(rows) == 3
    by_p = sorted(rows, key=lambda r: r["y_lo"])
    # outer-low bin (-inf, 99.5] → clipped to [50, 99.5]
    assert by_p[0]["y_lo"] == 50.0
    assert abs(by_p[0]["y_hi"] - 99.5) < 1e-9
    # middle bin (99.5, 101]
    assert abs(by_p[1]["y_lo"] - 99.5) < 1e-9
    assert abs(by_p[1]["y_hi"] - 101.0) < 1e-9
    # outer-high bin (101, +inf) → clipped to [101, 150]
    assert abs(by_p[2]["y_lo"] - 101.0) < 1e-9
    assert by_p[2]["y_hi"] == 150.0


def test_alpha_floor_and_cap():
    """Tiny-p bins floor at 0.03; dominant-p bins cap at 0.6 so the chart's
    bars + triangles remain legible."""
    D = _dashboard()
    d = _dist(anchor_price=100.0,
              xs=[-1.0, 0.0, 1.0],
              ps=[1e-4, 0.9998, 1e-4])  # one near-spike, two tails
    rows = D._distribution_band_rows([d], y_clip_lo=80.0, y_clip_hi=120.0)
    assert len(rows) == 3
    floors = [r["alpha"] for r in rows if r["p"] < D._BAND_ALPHA_FLOOR]
    assert all(a == D._BAND_ALPHA_FLOOR for a in floors)
    spikes = [r["alpha"] for r in rows if r["p"] > D._BAND_ALPHA_CAP]
    assert all(a == D._BAND_ALPHA_CAP for a in spikes)


def test_t_extent_from_horizon():
    """t_hi = submitted_at + horizon_minutes (via distribution_validator)."""
    from meta_agent.distribution_validator import horizon_to_minutes
    D = _dashboard()
    ts = datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc)
    d = _dist(anchor_price=100.0, xs=[-1.0, 0.0, 1.0], ps=[0.25, 0.5, 0.25],
              submitted_at=ts, horizon="1h")
    rows = D._distribution_band_rows([d], y_clip_lo=80.0, y_clip_hi=120.0)
    from datetime import timedelta
    expected_t_hi = ts + timedelta(minutes=horizon_to_minutes("1h"))
    for r in rows:
        assert r["t_lo"] == ts
        assert r["t_hi"] == expected_t_hi


def test_row_cap_keeps_newest_first():
    """When the band cap fires, the most-recent distributions should win out."""
    D = _dashboard()
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    from datetime import timedelta
    # Build N distributions each spanning many bins so we blow the cap fast.
    dists = []
    spacing = 0.5
    n_bins = 11
    xs = [-(n_bins // 2) * spacing + i * spacing for i in range(n_bins)]
    ps = [1.0 / n_bins] * n_bins
    n_dist = D._BAND_ROW_CAP // n_bins + 5  # exceed cap by a few full distributions
    for k in range(n_dist):
        ts = base + timedelta(minutes=k)
        dists.append(_dist(anchor_price=100.0, xs=xs, ps=ps,
                           submitted_at=ts, horizon="5m"))
    rows = D._distribution_band_rows(dists, y_clip_lo=80.0, y_clip_hi=120.0)
    assert len(rows) <= D._BAND_ROW_CAP
    # Newest-first: the freshest distribution's submitted_at should appear.
    newest = max(d["submitted_at"] for d in dists)
    survivors = {r["submitted_at"] for r in rows}
    assert newest in survivors


def test_skips_degenerate_distributions():
    D = _dashboard()
    base = datetime(2026, 5, 17, tzinfo=timezone.utc)
    bad = [
        # Single bin (< 2)
        {"submitted_at": base, "distribution": {"anchor_price": 100.0,
            "axis": "return_pct", "horizon": "1h",
            "bins": [{"x": 0.0, "p": 1.0}]}},
        # anchor_price <= 0
        _dist(anchor_price=0.0, xs=[-1.0, 0.0, 1.0], ps=[0.25, 0.5, 0.25]),
        # Missing distribution payload
        {"submitted_at": base, "distribution": None},
        # Missing submitted_at
        _dist(anchor_price=100.0, xs=[-1.0, 0.0, 1.0], ps=[0.25, 0.5, 0.25],
              submitted_at=None) | {"submitted_at": None},
    ]
    rows = D._distribution_band_rows(bad, y_clip_lo=80.0, y_clip_hi=120.0)
    assert rows == []


def test_log_return_axis_converted_to_pct():
    """log_return axis: x is dimensionless log-return; converted to percent
    via the small-x linear approx."""
    D = _dashboard()
    d = _dist(anchor_price=100.0, xs=[-0.01, 0.0, 0.01],
              ps=[0.25, 0.5, 0.25])
    d["distribution"]["axis"] = "log_return"
    rows = D._distribution_band_rows([d], y_clip_lo=80.0, y_clip_hi=120.0)
    by_p = sorted(rows, key=lambda r: r["y_center"])
    # mid bin: 100 * (1 + 0/100) = 100
    assert abs(by_p[1]["y_center"] - 100.0) < 1e-9
    # top bin: 100 * (1 + 1/100) = 101 (since 0.01 log → 1% via 100x)
    assert abs(by_p[2]["y_center"] - 101.0) < 1e-9
