"""Iron bootstrap quant: cycle_momentum.

Industrial / transport names trade on cycle inflections. This indicator looks
at the slope of the 50-bar SMA against the 200-bar SMA — a classic cycle
proxy — and converts it into a forecast tuple matching the new conviction
contract:

    {direction, conviction, expected_return_pct, time_to_target_days, inputs}

Iron may override this with LLM judgment in their review prompt; the model's
job is to give a starting point.
"""

from __future__ import annotations

from typing import Any


def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if len(bars) < 200:
        return {
            "direction": "flat",
            "conviction": 0.0,
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

    # Industrials are slow — assume 30-day horizon for the cycle move
    time_to_target_days = 30
    conviction = (e_return / time_to_target_days) if time_to_target_days else 0.0

    return {
        "direction": direction,
        "conviction": round(conviction, 4),
        "expected_return_pct": round(e_return if direction == "long" else -e_return, 3),
        "time_to_target_days": time_to_target_days,
        "inputs": {
            "sma50_now": round(sma50_now, 2),
            "sma200_now": round(sma200_now, 2),
            "spread_now": round(spread_now, 3),
            "spread_slope_10bar": round(spread_slope, 3),
            "spread_pct_of_price": round(spread_pct_of_price, 3),
        },
    }
