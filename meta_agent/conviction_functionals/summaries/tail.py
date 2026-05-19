"""Tail probabilities. THRESHOLD is in axis units (e.g. percent return)."""
from __future__ import annotations

THRESHOLD = 0.5  # default ±0.5% — combiners that need a wider tail override


def compute_positive(xs: list[float], ps: list[float], threshold: float = THRESHOLD) -> float:
    return sum(p for x, p in zip(xs, ps) if x > threshold)


def compute_negative(xs: list[float], ps: list[float], threshold: float = THRESHOLD) -> float:
    return sum(p for x, p in zip(xs, ps) if x < -threshold)
