from __future__ import annotations
from typing import Any

MODEL_VERSION = "1.1"

def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if len(bars) < 50:
        return {
            "direction": "flat",
            "likelihood": 0.0,
            "expected_return_pct": 0.0,
            "time_to_target_days": 0,
            "inputs": {"reason": f"need >=50 bars, got {len(bars)}"},
        }

    closes = [b["c"] for b in bars]
    sma20 = sum(closes[-20:]) / 20.0
    sma20_prev = sum(closes[-25:-5]) / 20.0
    last = closes[-1]

    sma_slope_pct = ((sma20 - sma20_prev) / sma20_prev * 100) if sma20_prev else 0.0
    dist_pct = ((last - sma20) / sma20 * 100) if sma20 else 0.0

    # Long when both slope and price are above SMA; short when both below.
    if sma_slope_pct > 0.5 and dist_pct > 0:
        direction = "long"
        e_return = min(8.0, abs(sma_slope_pct) * 2.0 + abs(dist_pct) * 0.5)
    elif sma_slope_pct < -0.5 and dist_pct < 0:
        direction = "short"
        e_return = min(8.0, abs(sma_slope_pct) * 2.0 + abs(dist_pct) * 0.5)
    else:
        direction = "flat"
        e_return = 0.0

    time_to_target_days = 14
    likelihood = (e_return / time_to_target_days) if time_to_target_days else 0.0

    # Add sensitivity to underlying sector ETFs (e.g., SMH, SOXX) as a
    # signal for momentum validity.
    if symbol in context.get('sector_etfs', []):
        # For ETFs, expected_return_pct and likelihood are adjusted.
        # For example: SMH shows a long bias if underlying designers are trending up.
        if direction == 'long':
            e_return = min(5.0, e_return * 1.2)
            likelihood = (e_return / time_to_target_days) if time_to_target_days else 0.0
        elif direction == 'short':
            e_return = max(-5.0, e_return * 1.2)
            likelihood = (abs(e_return) / time_to_target_days) if time_to_target_days else 0.0

    return {
        "direction": direction,
        "likelihood": round(likelihood, 4),
        "expected_return_pct": round(e_return if direction == "long" else -e_return, 3),
        "time_to_target_days": time_to_target_days,
        "inputs": {
            "sma20": round(sma20, 2),
            "sma20_slope_pct_5bar": round(sma_slope_pct, 3),
            "dist_above_sma_pct": round(dist_pct, 3),
            "note": "Cross-check vs hyperscaler capex commentary and TSM utilization (Fab agent).",
        },
    }
