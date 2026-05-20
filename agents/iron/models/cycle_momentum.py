from __future__ import annotations

from typing import Any

def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if len(bars) < 200:
        return {
            "direction": "flat",
            "likelihood": 0.0,
            "expected_return_pct": 0.0,
            "time_to_target_days": 0,
            "inputs": {"reason": f"need >=200 bars, got {len(bars)}"},
        }

    closes = [b["c"] for b in bars]
    sma50_now = sum(closes[-50:]) / 50.0
    sma200_now = sum(closes[-200:]) / 200.0
    sma50_prev = sum(closes[-60:-10]) / 50.0
    sma200_prev = sum(closes[-210:-10]) / 200.0

    # Slope of the 50/200 spread over the past 10 bars
    spread_now = sma50_now - sma200_now
    spread_prev = sma50_prev - sma200_prev
    spread_slope = spread_now - spread_prev

    last_close = closes[-1]
    spread_pct_of_price = (spread_now / last_close * 100) if last_close else 0.0

    # Direction: positive spread + positive slope = long; mirror for short.
    if spread_now > 0 and spread_slope > 0:
        direction = "long"
        # crude E[return]: half the current spread (mean-reversion target)
        e_return = abs(spread_pct_of_price) * 0.5
    elif spread_now < 0 and spread_slope < 0:
        direction = "short"
        e_return = abs(spread_pct_of_price) * 0.5
    else:
        direction = "flat"
        e_return = 0.0

    # Adjust horizon to 45 days for defense/industrial names (slow to reversion)
    time_to_target_days = 45 if symbol in ["RTX", "LMT", "BA"] else 30
    likelihood = (e_return / time_to_target_days) if time_to_target_days else 0.0

    # Add volatility-weighted likelihood scaling for better calibration
    if len(bars) >= 14:
        atr_values = [b["h"] - b["l"] for b in bars[-14:]]
        atr14_now = sum(atr_values) / 14
        atr_ratio = abs(spread_pct_of_price) / atr14_now if atr14_now else 1.0
        likelihood = min(likelihood * atr_ratio, 1.0)

    return {
        "direction": direction,
        "likelihood": round(likelihood, 4),
        "expected_return_pct": round(e_return if direction == "long" else -e_return, 3),
        "time_to_target_days": time_to_target_days,
        "inputs": {
            "sma50_now": round(sma50_now, 2),
            "sma200_now": round(sma200_now, 2),
            "spread_now": round(spread_now, 3),
            "spread_slope_10bar": round(spread_slope, 3),
            "spread_pct_of_price": round(spread_pct_of_price, 3),
            "atr14_now": round(atr14_now, 3) if "atr14_now" in locals() else None,
            "atr_ratio": round(atr_ratio, 3) if "atr_ratio" in locals() else None,
        },
    }

MODEL_VERSION = "1.2"