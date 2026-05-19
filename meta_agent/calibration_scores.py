"""Proper scoring rules for discrete probabilistic forecasts.

Applied by the forecast resolver once a forecast's horizon elapses and a
realized return is known. All inputs are validated distributions (per
meta_agent.distribution_validator): uniformly-spaced bins, p ≥ 1e-4,
sum(p) ≈ 1, sorted x. Outputs are scalars (smaller = better for log-loss /
Brier / CRPS / pinball; for Sharpe-of-skill, computed at aggregation time).

Functions:
  realized_bin_index(xs, realized)  → int (clipped to [0, n-1])
  log_loss(ps, realized_idx)        → -log(p[realized_idx])
  brier(ps, realized_idx)           → Σ (p_i - 𝟙[i=realized])²
  crps_step_cdf(xs, ps, realized)   → CRPS via Hersbach 2000 step-CDF form
  pinball(xs, ps, realized, alpha)  → pinball loss at quantile α

The bin index conventions:
  - Each bin's "x" is the bin center.
  - A bin covers [x - spacing/2, x + spacing/2).
  - For realized < xs[0] - spacing/2, idx = 0 (left-clipped).
  - For realized > xs[-1] + spacing/2, idx = n-1 (right-clipped).
"""
from __future__ import annotations

import math
from bisect import bisect_right


def _bin_spacing(xs: list[float]) -> float:
    return xs[1] - xs[0] if len(xs) >= 2 else 1.0


def realized_bin_index(xs: list[float], realized: float) -> int:
    """Return the bin index (0..n-1) containing the realized value. Clips at
    the endpoints — out-of-range realizations attribute mass to the nearest
    boundary bin (a heavy tail will show up in the calibration curve)."""
    if not xs:
        raise ValueError("empty xs")
    spacing = _bin_spacing(xs)
    half = spacing / 2.0
    # Bin centers are xs[i]; edge between bin i and i+1 is xs[i] + half.
    # Convention: half-open [lo, hi) — value exactly at xs[i]+half belongs to
    # bin i+1, so use bisect_right.
    edges = [xs[i] + half for i in range(len(xs) - 1)]
    idx = bisect_right(edges, realized)
    return max(0, min(len(xs) - 1, idx))


def log_loss(ps: list[float], realized_idx: int) -> float:
    """-log p[realized_idx]. The validator enforces p ≥ 1e-4, so this is
    always finite (max value ≈ -log(1e-4) ≈ 9.21)."""
    p = max(ps[realized_idx], 1e-12)
    return -math.log(p)


def brier(ps: list[float], realized_idx: int) -> float:
    """Multi-class Brier: Σ_i (p_i - 𝟙[i=realized])². Range [0, 2)."""
    out = 0.0
    for i, p in enumerate(ps):
        target = 1.0 if i == realized_idx else 0.0
        out += (p - target) ** 2
    return out


def crps_step_cdf(xs: list[float], ps: list[float], realized: float) -> float:
    """Continuous Ranked Probability Score for a discrete distribution.

    The forecast CDF F is right-continuous with jumps at each bin center:
        F(x) = 0                       for x < x_0
        F(x) = cdf[i] = Σ_{j≤i} p_j    for x_i ≤ x < x_{i+1}
        F(x) = 1                       for x ≥ x_{n-1}

    CRPS = ∫ (F(x) - 𝟙[x ≥ realized])² dx, evaluated on the truncated support
    [x_0 - s/2, x_{n-1} + s/2] (s = bin spacing) plus penalty for realized
    values that fall outside support: (gap) × 1 per unit, since F and 𝟙
    disagree by 1 over that entire region.

    Implementation: F and the indicator are both piecewise constant; their
    only breakpoints are the bin centers + the realized value. So we sort
    breakpoints, walk consecutive pairs, and accumulate (F - I)² × width on
    each. This is the exact Hersbach (2000) reduction for a discrete CDF.
    """
    if not xs:
        return 0.0
    s = _bin_spacing(xs)
    support_lo = xs[0] - s / 2.0
    support_hi = xs[-1] + s / 2.0

    # Right-continuous CDF: cdf[i] = sum p_j for j ≤ i
    cdf: list[float] = []
    acc = 0.0
    for p in ps:
        acc += p
        cdf.append(acc)

    # Out-of-support tail contributions
    crps = 0.0
    if realized < support_lo:
        crps += support_lo - realized   # F=0, I=1 over the gap → (0-1)² × gap
    elif realized > support_hi:
        crps += realized - support_hi   # F=1, I=0 over the gap → (1-0)² × gap

    # Clamp r into the support window for the interior integration; the gap
    # has already paid for the discrepancy outside.
    r = min(max(realized, support_lo), support_hi)

    # Breakpoints inside [support_lo, support_hi]: support endpoints, the bin
    # centers (CDF jumps), and r (indicator jump). Sort + dedupe.
    pts = sorted({support_lo, support_hi, r, *xs})

    def cdf_at(x: float) -> float:
        # Right-continuous: F(x) = cdf[i] where i = largest s.t. xs[i] ≤ x
        # (or 0 if x < xs[0]).
        if x < xs[0]:
            return 0.0
        # Linear scan is fine for n ≤ 20.
        for i in range(len(xs) - 1, -1, -1):
            if xs[i] <= x:
                return cdf[i]
        return 0.0

    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        if b <= a:
            continue
        # Sample F + indicator at the midpoint of the segment. Both are
        # constant inside (a, b) by construction.
        mid = (a + b) / 2.0
        F = cdf_at(mid)
        I = 1.0 if mid >= r else 0.0
        crps += (b - a) * (F - I) ** 2

    return crps


def _quantile_from_discrete(xs: list[float], ps: list[float], alpha: float) -> float:
    """Return the α-quantile of the discrete distribution. Linear interpolation
    inside the bin where the cumulative mass crosses α."""
    if not xs:
        return 0.0
    s = _bin_spacing(xs)
    cum = 0.0
    for x, p in zip(xs, ps):
        if cum + p >= alpha:
            # Linear interp within this bin's width
            remainder = alpha - cum
            frac = remainder / max(p, 1e-12)
            return (x - s / 2.0) + frac * s
        cum += p
    return xs[-1] + s / 2.0


def pinball(xs: list[float], ps: list[float], realized: float, alpha: float) -> float:
    """Pinball loss at quantile α ∈ (0, 1). Standard form:
        ρ_α(y, q) = (α - 𝟙[y < q]) (y - q)
    where q = α-quantile of the predicted distribution.
    Penalizes under-prediction at high α and over-prediction at low α —
    directly tests tail calibration.
    """
    q = _quantile_from_discrete(xs, ps, alpha)
    indicator = 1.0 if realized < q else 0.0
    return (alpha - indicator) * (realized - q)


def score_distribution(
    distribution: dict, realized: float
) -> dict[str, float | int]:
    """Convenience: compute all five scores for a stored distribution against
    a realized return. Returns {realized_bin_idx, log_loss, brier, crps,
    pinball05, pinball95}."""
    bins = distribution.get("bins") or []
    xs = [float(b["x"]) for b in bins]
    ps = [float(b["p"]) for b in bins]
    if not xs:
        return {}
    idx = realized_bin_index(xs, realized)
    return {
        "realized_bin_idx": idx,
        "log_loss": log_loss(ps, idx),
        "brier": brier(ps, idx),
        "crps": crps_step_cdf(xs, ps, realized),
        "pinball05": pinball(xs, ps, realized, 0.05),
        "pinball95": pinball(xs, ps, realized, 0.95),
    }
