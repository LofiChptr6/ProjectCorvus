"""Distribution-mixer unit tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from meta_agent import conviction_functionals, distribution_mixer


def _make_dist(mu: float, sigma: float, horizon: str = "1h",
               ts: datetime | None = None, anchor_price: float = 100.0) -> dict:
    spacing = 0.5
    n = 11
    lo = mu - 5 * spacing
    centers = [lo + i * spacing for i in range(n)]
    # Crude Gaussian on the grid; renormalize.
    import math
    raw = [math.exp(-0.5 * ((c - mu) / max(sigma, 1e-6)) ** 2) for c in centers]
    s = sum(raw)
    probs = [max(p / s, 1e-4) for p in raw]
    s2 = sum(probs)
    probs = [p / s2 for p in probs]
    return {
        "anchor_price": anchor_price,
        "anchor_ts": (ts or datetime.now(timezone.utc)).isoformat(),
        "axis": "return_pct",
        "horizon": horizon,
        "bins": [{"x": round(c, 4), "p": round(p, 6)} for c, p in zip(centers, probs)],
        "model": "test",
        "model_version": "0",
    }


def test_freshness_gate_filters_stale_distributions():
    stale = _make_dist(1.0, 0.5, horizon="5m",
                      ts=datetime.now(timezone.utc) - timedelta(minutes=5))
    # 5m horizon × 0.3 = 1.5min freshness window; 5min-old fails the gate.
    assert not distribution_mixer._is_fresh(stale)


def test_freshness_gate_passes_fresh_distributions():
    fresh = _make_dist(1.0, 0.5, horizon="1d")
    assert distribution_mixer._is_fresh(fresh)


def test_build_mixture_returns_none_when_all_stale():
    stale = _make_dist(1.0, 0.5, horizon="5m",
                      ts=datetime.now(timezone.utc) - timedelta(hours=2))
    out = distribution_mixer.build_mixture([(stale, 1.0)])
    assert out is None


def test_build_mixture_centers_on_weighted_mean():
    d_pos = _make_dist(2.0, 0.5)
    d_neg = _make_dist(-2.0, 0.5)
    mixture = distribution_mixer.build_mixture([(d_pos, 1.0), (d_neg, 1.0)])
    assert mixture is not None
    xs = [b["x"] for b in mixture["bins"]]
    ps = [b["p"] for b in mixture["bins"]]
    mu = sum(x * p for x, p in zip(xs, ps))
    # Bimodal mixture symmetric around 0
    assert abs(mu) < 0.5


def test_mixture_conviction_emits_direction_and_scalar():
    d_pos = _make_dist(1.5, 0.3)
    rows = [{"agent_name": "atlas", "distribution": d_pos,
             "time_to_target_days": 1, "horizon": "1h"}]
    res = distribution_mixer.mixture_conviction(rows)
    assert res is not None
    direction, scalar, contribs = res
    assert direction == "long"
    assert 0 < scalar <= 1
    assert ("atlas", 1.0) in contribs or any(c[0] == "atlas" for c in contribs)


def test_mixture_with_functional_swap():
    d = _make_dist(1.5, 0.3)
    rows = [{"agent_name": "atlas", "distribution": d,
             "time_to_target_days": 1, "horizon": "1h"}]
    res_default = distribution_mixer.mixture_conviction(rows)
    res_kelly = distribution_mixer.mixture_conviction(rows, functional_name="frac_kelly")
    assert res_default is not None and res_kelly is not None
    # Different functional → different conviction (or at least not guaranteed equal)
    assert res_default[0] == res_kelly[0]  # same direction
