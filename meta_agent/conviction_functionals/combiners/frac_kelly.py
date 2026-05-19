"""Fractional Kelly: c = clip(0.25 · μ / σ², 0, 1).

The 0.25 fraction is the live-desk default — full Kelly is too aggressive
for any realistic estimation-error budget on financial returns.
"""
from __future__ import annotations


KELLY_FRACTION = 0.25


def compute(summaries: dict, t_days: float) -> float:
    mu = summaries["mean"]
    var = max(summaries["variance"], 1e-9)  # avoid div-by-zero on collapsed dists
    raw = KELLY_FRACTION * mu / var
    return max(0.0, min(1.0, raw))
