from __future__ import annotations

from typing import Any

MODEL_VERSION = '1.1'

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
    sma50_prev = sum(closes[-70:-20]) / 50.0
    sma200_prev = sum(closes[-220:-20]) / 200.0

    spread_now = sma50_now - sma200_now
    spread_prev = sma50_prev - sma200_prev
    spread_slope = spread_now - spread_prev

    last_close = closes[-1]
    spread_pct_of_price = (spread_now / last_close * 100) if last_close else 0.0

    # Graded score (replaces previous binary AND-gate). Weighted combination of
    # spread level and slope direction; sign drives direction, magnitude drives
    # expected return. This gives directional signals on names where one factor
    # (level OR momentum) is constructive but the other is just neutral —
    # capturing "weakly bullish on a stalling spread" or "fading uptrend that
    # hasn't yet rolled over" instead of forcing them to flat.
    slope_pct_of_price = (spread_slope / last_close * 100) if last_close else 0.0
    score = 0.5 * spread_pct_of_price + 0.5 * slope_pct_of_price * 3.0   # slope weighted up since it's smaller-magnitude

    # Threshold below which the signal is too weak to act on
    THRESHOLD = 0.5
    if score > THRESHOLD:
        direction = "long"
        e_return = score * 0.5   # 50% confidence haircut
    elif score < -THRESHOLD:
        direction = "short"
        e_return = score * 0.5   # already negative
    else:
        direction = "flat"
        e_return = 0.0

    time_to_target_days = 90
    likelihood = (abs(e_return) / time_to_target_days) if time_to_target_days else 0.0

    return {
        "direction": direction,
        "likelihood": round(likelihood, 4),
        "expected_return_pct": round(e_return, 3),   # already signed
        "time_to_target_days": time_to_target_days,
        "inputs": {
            "sma50_now": round(sma50_now, 2),
            "sma200_now": round(sma200_now, 2),
            "spread_now": round(spread_now, 3),
            "spread_slope_20bar": round(spread_slope, 3),
            "spread_pct_of_price": round(spread_pct_of_price, 3),
            "slope_pct_of_price": round(slope_pct_of_price, 4),
            "score": round(score, 3),
            "note": "Capex cycle leads ~6-12mo; cross-check vs TSM CapEx guide and ASML book-to-bill."
        },
    }