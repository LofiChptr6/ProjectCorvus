"""Default combiner: c = |E[r]| · concentration / √t_days.

Rationale: rewards both expected magnitude and certainty; penalizes long
horizons (per Sharpe-like time scaling). Units roughly match the legacy
0..1 conviction scalar after normalization, so existing influence weights
and max_per_symbol remain meaningful when this is the active functional.
"""
from __future__ import annotations


def compute(summaries: dict, t_days: float) -> float:
    mu = summaries["mean"]
    conc = summaries["concentration"]
    denom = max(t_days, 1e-6) ** 0.5
    return mu * conc / denom
