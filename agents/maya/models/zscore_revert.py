"""Maya bootstrap indicator: zscore_revert.

Computes the close's z-score against the prior 20-bar mean and stdev. |z| > 2 is the
classic mean-reversion setup. Sign indicates direction of expected reversion.
"""

from __future__ import annotations

from math import sqrt
from typing import Any


def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if len(bars) < 22:
        return {"signal": None, "reason": f"need >=22 bars, got {len(bars)}"}

    closes = [b["c"] for b in bars[-21:-1]]
    last_close = bars[-1]["c"]
    mean = sum(closes) / len(closes)
    var = sum((c - mean) ** 2 for c in closes) / len(closes)
    stdev = sqrt(var) if var > 0 else 0.0
    if stdev == 0:
        return {"signal": None, "reason": "zero stdev — flat window"}

    z = (last_close - mean) / stdev
    bias = "fade_long" if z > 2 else "fade_short" if z < -2 else "no_trade"

    if z > 2:
        direction = "short"
        e_return = max(-z * 1.5, -6.0)
    elif z < -2:
        direction = "long"
        e_return = min(-z * 1.5, 6.0)
    else:
        direction = "flat"
        e_return = 0.0
    horizon = 5
    likelihood = round(abs(e_return) / horizon, 4) if horizon else 0.0

    return {
        "signal": round(-z, 3),
        "zscore": round(z, 3),
        "mean_20": round(mean, 2),
        "stdev_20": round(stdev, 3),
        "last_close": last_close,
        "bias": bias,
        "interpretation": (
            f"{symbol} is {abs(z):.2f}σ {'above' if z > 0 else 'below'} 20d mean — "
            f"{'fade-the-rally' if z > 2 else 'buy-the-dip' if z < -2 else 'no edge'}"
        ),
        "direction": direction,
        "likelihood": likelihood,
        "expected_return_pct": round(e_return, 3),
        "time_to_target_days": horizon,
        "inputs": {"z": round(z, 3), "mean_20": round(mean, 2), "stdev_20": round(stdev, 3)},
    }
