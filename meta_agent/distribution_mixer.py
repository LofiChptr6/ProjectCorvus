"""Build a weighted-mixture distribution from per-agent forecast distributions.

The allocator's distribution-aware path (off by default; flip via desk policy)
collapses multiple agents' beliefs on the same symbol into a single mixture
before applying the conviction functional. This preserves the orthogonal
information each agent's distribution carries instead of just summing scalars.

Architecture:
  1. Fetch active distribution rows (db.store.get_active_distributions).
  2. Apply per-row weight = influence × freshness × inverse-horizon
     (freshness checks the anchor-staleness gate: skip rows where
     now-anchor_ts > MAX_STALENESS_RATIO · horizon_minutes).
  3. Re-bin each input distribution onto a common reference grid
     (RETURN_PCT_GRID — 21 bins from -5% to +5%) via piecewise overlap.
  4. Sum weighted re-binned probabilities and renormalize.
  5. Run the named conviction functional on the mixture at the longest
     horizon present; return a (direction, conviction, contributors) tuple.

The mixer assumes axis="return_pct" — log_return distributions are converted
via the linear approximation 100·log_return ≈ return_pct.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from meta_agent import conviction_functionals
from meta_agent.distribution_validator import horizon_to_minutes

# Reference grid: 21 uniformly-spaced bins on return_pct from -5% to +5%.
# Spans typical 1d / 1w moves with enough granularity for the conviction
# functional's summaries (mean, variance, concentration) to differ meaningfully
# across models without making the grid so dense that re-binning ε errors
# dominate.
REF_GRID_LO = -5.0
REF_GRID_HI = 5.0
REF_GRID_N = 21
MAX_STALENESS_RATIO = 0.3   # skip if now - anchor_ts > 0.3 * horizon_minutes
SMOOTHING_FLOOR = 1.0e-4


def reference_grid() -> tuple[list[float], float]:
    spacing = (REF_GRID_HI - REF_GRID_LO) / (REF_GRID_N - 1)
    centers = [REF_GRID_LO + i * spacing for i in range(REF_GRID_N)]
    return centers, spacing


def _is_fresh(distribution: dict, now: datetime | None = None) -> bool:
    ts_str = distribution.get("anchor_ts")
    if not ts_str:
        return False
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    age_min = (now - ts).total_seconds() / 60.0
    horizon = distribution.get("horizon", "1d")
    max_age = MAX_STALENESS_RATIO * horizon_to_minutes(horizon)
    return age_min <= max_age


def _rebin_to_reference(distribution: dict) -> list[float]:
    """Project a distribution's bins onto the reference grid via piecewise
    overlap. Returns a list of length REF_GRID_N with probabilities (not
    necessarily summing to 1 if the distribution's support exceeds the
    reference range — caller renormalizes the full mixture)."""
    centers, spacing = reference_grid()
    half = spacing / 2.0
    # Reference bin [ref_centers[i] - half, ref_centers[i] + half)
    out = [0.0] * REF_GRID_N

    bins = distribution.get("bins") or []
    if not bins:
        return out
    # Each input bin has an x (center) and a p; assume uniform spacing in xs
    xs = [float(b["x"]) for b in bins]
    ps = [float(b["p"]) for b in bins]
    if len(xs) < 2:
        return out
    # If axis is log_return, convert to return_pct via 100*log_return ≈ return_pct.
    axis = distribution.get("axis", "return_pct")
    if axis == "log_return":
        xs = [100.0 * x for x in xs]
    in_spacing = xs[1] - xs[0]
    in_half = in_spacing / 2.0

    for x_c, p in zip(xs, ps):
        in_lo = x_c - in_half
        in_hi = x_c + in_half
        # Distribute p across all reference bins overlapped by [in_lo, in_hi].
        for j, ref_c in enumerate(centers):
            ref_lo = ref_c - half
            ref_hi = ref_c + half
            overlap = min(in_hi, ref_hi) - max(in_lo, ref_lo)
            if overlap <= 0:
                continue
            out[j] += p * (overlap / in_spacing)
    return out


def build_mixture(weighted_distributions: Iterable[tuple[dict, float]]) -> dict | None:
    """Build a weighted mixture of distributions on the reference grid.

    weighted_distributions: iterable of (distribution_dict, weight) pairs.
    Returns a dict in distribution-payload shape (with model='mixture',
    bins on the reference grid). None if no inputs survive the freshness
    gate or all weights are 0.
    """
    centers, _ = reference_grid()
    acc = [0.0] * REF_GRID_N
    total_weight = 0.0
    survivors = 0
    longest_horizon_min = 0
    anchor_price = None

    for dist, weight in weighted_distributions:
        if weight <= 0:
            continue
        if not _is_fresh(dist):
            continue
        rebinned = _rebin_to_reference(dist)
        if sum(rebinned) <= 0:
            continue
        for i, p in enumerate(rebinned):
            acc[i] += weight * p
        total_weight += weight
        survivors += 1
        h_min = horizon_to_minutes(dist.get("horizon", "1d"))
        if h_min > longest_horizon_min:
            longest_horizon_min = h_min
            anchor_price = dist.get("anchor_price")

    if survivors == 0 or total_weight == 0:
        return None

    # Renormalize and apply smoothing floor.
    s = sum(acc)
    if s <= 0:
        return None
    probs = [max(p / s, SMOOTHING_FLOOR) for p in acc]
    s2 = sum(probs)
    probs = [p / s2 for p in probs]

    bins = [{"x": round(c, 4), "p": round(p, 6)} for c, p in zip(centers, probs)]
    horizon_label = _minutes_to_horizon_label(longest_horizon_min)
    return {
        "anchor_price": anchor_price,
        "anchor_ts": datetime.now(timezone.utc).isoformat(),
        "axis": "return_pct",
        "horizon": horizon_label,
        "bins": bins,
        "model": "mixture",
        "model_version": "1.0",
    }


def _minutes_to_horizon_label(minutes: int) -> str:
    for label in ("1w", "1d", "1h", "5m"):
        if horizon_to_minutes(label) <= minutes:
            return label
    return "1d"


async def fetch_and_mix_symbol(
    symbol: str,
    influence_by_agent: dict[str, float] | None = None,
    functional_name: str | None = None,
) -> tuple[str, float, list[tuple[str, float]]] | None:
    """Convenience wrapper: fetch all active distributions for a symbol from
    agent_forecast and feed them through `mixture_conviction`. Returns None
    if no distributions exist (caller falls back to scalar-sum aggregation
    on agent_conviction)."""
    from db import store
    rows = await store.get_active_distributions(symbol=symbol)
    if not rows:
        return None
    return mixture_conviction(rows, influence_by_agent=influence_by_agent,
                              functional_name=functional_name)


def mixture_conviction(
    distributions: list[dict],
    influence_by_agent: dict[str, float] | None = None,
    functional_name: str | None = None,
) -> tuple[str, float, list[tuple[str, float]]] | None:
    """End-to-end: take a list of distribution rows (each carrying agent_name,
    distribution payload, horizon, time_to_target_days) and return the
    (direction, conviction, contributors) for the symbol.

    Returns None if no fresh inputs survived.
    """
    influence_by_agent = influence_by_agent or {}
    functional_name = functional_name or conviction_functionals.DEFAULT_FUNCTIONAL

    weighted_pairs: list[tuple[dict, float]] = []
    contributors: list[tuple[str, float]] = []
    for row in distributions:
        dist = row.get("distribution")
        if isinstance(dist, str):
            import json as _json
            dist = _json.loads(dist)
        if not dist:
            continue
        agent = row.get("agent_name", "unknown")
        ttd = max(float(row.get("time_to_target_days", 1) or 1), 1.0)
        inf = influence_by_agent.get(agent, 1.0)
        # Weight = influence × 1/√ttd (Sharpe-scaling per the plan)
        w = inf / (ttd ** 0.5)
        weighted_pairs.append((dist, w))
        contributors.append((agent, w))

    mixture = build_mixture(weighted_pairs)
    if mixture is None:
        return None

    # Run functional on the mixture at the longest horizon's ttd-days.
    # Use the horizon label embedded in the mixture for consistency.
    ttd_days = max(
        float(row.get("time_to_target_days", 1) or 1) for row in distributions
    )
    scalar = conviction_functionals.run(functional_name, mixture, ttd_days)
    # Direction = sign of E[r] under the mixture.
    xs = [float(b["x"]) for b in mixture["bins"]]
    ps = [float(b["p"]) for b in mixture["bins"]]
    mu = sum(x * p for x, p in zip(xs, ps))
    if abs(mu) < 1e-9:
        direction = "flat"
    elif mu > 0:
        direction = "long"
    else:
        direction = "short"
    return direction, scalar, contributors
