"""Conditional Value-at-Risk (CVaR / expected shortfall).

For a discrete distribution sorted by x, CVaR at level α is the expectation
of x conditioned on x being in the worst α-tail. Implemented for the *upside*
(loss to bears = gain to bulls) — caller flips sign if needed.
"""
from __future__ import annotations


def _compute(xs: list[float], ps: list[float], alpha: float, tail: str) -> float:
    if not xs:
        return 0.0
    # Sort by x (ascending). xs already sorted per validator but be safe.
    paired = sorted(zip(xs, ps), key=lambda kv: kv[0])
    if tail == "upper":
        paired = list(reversed(paired))
    cum_p = 0.0
    weighted_sum = 0.0
    for x, p in paired:
        take = min(p, alpha - cum_p)
        if take <= 0:
            break
        weighted_sum += x * take
        cum_p += take
        if cum_p >= alpha:
            break
    if cum_p == 0:
        return 0.0
    return weighted_sum / cum_p


def compute_95(xs: list[float], ps: list[float]) -> float:
    """E[r | r in upper 5%] — upside CVaR at α=0.05."""
    return _compute(xs, ps, alpha=0.05, tail="upper")
