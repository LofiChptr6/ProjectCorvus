"""Normalized concentration ∈ [0, 1]:  1 - H(p) / log(n).

0 = perfectly diffuse (uniform); 1 = all mass on one bin.
Robust to bin count differences across models.
"""
from __future__ import annotations

from math import log


def compute(xs: list[float], ps: list[float]) -> float:
    n = len(ps)
    if n <= 1:
        return 0.0
    log_n = log(n)
    if log_n == 0:
        return 0.0
    # All p ≥ 1e-4 per validator, so log(p) is finite.
    h = -sum(p * log(p) for p in ps if p > 0)
    return max(0.0, min(1.0, 1.0 - h / log_n))
