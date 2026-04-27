"""Rex bootstrap indicator: breakout_strength.

Measures how decisively price is breaking above the prior 20-bar high, weighted by
recent volume relative to the 20-bar average. Higher = stronger breakout.

This is a starting point — Rex can rewrite or extend during evening reviews.
"""

from __future__ import annotations

from typing import Any


def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if len(bars) < 22:
        return {"signal": None, "reason": f"need >=22 bars, got {len(bars)}"}

    last = bars[-1]
    prior = bars[-21:-1]  # 20 bars excluding the latest

    prior_high = max(b["h"] for b in prior)
    last_close = last["c"]
    last_volume = last["v"] or 0
    avg_volume = sum(b["v"] or 0 for b in prior) / 20.0

    pct_above_high = (last_close - prior_high) / prior_high * 100 if prior_high else 0.0
    volume_ratio = (last_volume / avg_volume) if avg_volume else 0.0
    strength = pct_above_high * volume_ratio  # signs: positive only when breaking up

    if strength > 1.0:
        direction = "long"
        e_return = min(strength * 1.5, 8.0)
    elif pct_above_high < -1.0 and volume_ratio > 1.0:
        direction = "short"
        e_return = max(pct_above_high * volume_ratio * 0.5, -6.0)
    else:
        direction = "flat"
        e_return = 0.0
    horizon = 7
    conviction = round(abs(e_return) / horizon, 4) if horizon else 0.0

    return {
        "signal": round(strength, 3),
        "pct_above_prior_20d_high": round(pct_above_high, 3),
        "volume_ratio_vs_20d_avg": round(volume_ratio, 2),
        "last_close": last_close,
        "prior_20d_high": prior_high,
        "interpretation": (
            "strong breakout" if strength > 2.0
            else "weak breakout / no breakout" if strength < 0.5
            else "developing breakout"
        ),
        "direction": direction,
        "conviction": conviction,
        "expected_return_pct": round(e_return, 3),
        "time_to_target_days": horizon,
        "inputs": {
            "pct_above_prior_high": round(pct_above_high, 3),
            "volume_ratio": round(volume_ratio, 2),
        },
    }
