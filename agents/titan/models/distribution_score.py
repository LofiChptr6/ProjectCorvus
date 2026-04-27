"""Titan bootstrap indicator: distribution_score.

Counts lower-highs in the last 10 bars and flags negative-divergence-style weakness.
Higher absolute value = more distribution. Negative score = bearish setup.
"""

from __future__ import annotations

from typing import Any


def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if len(bars) < 12:
        return {"signal": None, "reason": f"need >=12 bars, got {len(bars)}"}

    recent = bars[-10:]
    highs = [b["h"] for b in recent]
    closes = [b["c"] for b in recent]

    lower_highs = sum(1 for i in range(1, len(highs)) if highs[i] < highs[i - 1])
    closes_below_open = sum(1 for b in recent if b["c"] < b["o"])
    range_compression = (max(highs) - min(b["l"] for b in recent)) / closes[-1] if closes[-1] else 0.0

    # Score: negative when distribution is heavy.
    score = -(lower_highs / 9.0) * 0.6 - (closes_below_open / 10.0) * 0.4
    score = round(score, 3)

    if score < -0.5:
        direction = "short"
        e_return = max(score * 8.0, -8.0)
    elif score > 0.3:
        direction = "long"
        e_return = min(score * 8.0, 5.0)
    else:
        direction = "flat"
        e_return = 0.0
    horizon = 10
    conviction = round(abs(e_return) / horizon, 4) if horizon else 0.0

    return {
        "signal": score,
        "lower_highs_in_10": lower_highs,
        "down_closes_in_10": closes_below_open,
        "range_compression": round(range_compression, 3),
        "last_close": closes[-1],
        "interpretation": (
            "heavy distribution / lean short" if score < -0.6
            else "consolidation / no edge" if score > -0.3
            else "mild distribution"
        ),
        "direction": direction,
        "conviction": conviction,
        "expected_return_pct": round(e_return, 3),
        "time_to_target_days": horizon,
        "inputs": {
            "score": score,
            "lower_highs": lower_highs,
            "closes_below_open": closes_below_open,
        },
    }
