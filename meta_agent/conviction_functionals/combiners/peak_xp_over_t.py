"""Literal user-spec combiner: c = sign(μ) · peak(p) · |μ| / t_days.

Included so the replay harness can compare against the user's original
hand-crafted form. The plan critique flagged the literal `peak(x·p)/t` as
ill-defined for negative x; this form uses the expected-return magnitude
weighted by the peak probability mass.
"""
from __future__ import annotations


def compute(summaries: dict, t_days: float) -> float:
    mu = summaries["mean"]
    pk = summaries["peak"]
    return mu * pk / max(t_days, 1e-6)
