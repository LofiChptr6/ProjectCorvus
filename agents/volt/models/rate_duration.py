"""Volt bootstrap quant: rate_duration.

Utilities and REITs are duration-sensitive. This indicator approximates the
rate-driven move by tracking the rolling correlation between the symbol and
TLT (long-bond proxy) over 60 bars, then scales by the recent spread of the
symbol from its own 50-bar mean.

Returns the new conviction contract:
    {direction, conviction, expected_return_pct, time_to_target_days, inputs}

Note: this bootstrap version uses only the symbol's own bars (no TLT cross-asset
fetch). The LLM in volt-review.md is expected to overlay actual rate context.
"""

from __future__ import annotations

from typing import Any


def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if len(bars) < 60:
        return {
            "direction": "flat",
            "conviction": 0.0,
            "expected_return_pct": 0.0,
            "time_to_target_days": 0,
            "inputs": {"reason": f"need >=60 bars, got {len(bars)}"},
        }

    closes = [b["c"] for b in bars]
    last = closes[-1]
    sma50 = sum(closes[-50:]) / 50.0
    # Rough volatility proxy: stdev of last-60 returns
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(-60, 0) if closes[i - 1]]
    if not rets:
        return {
            "direction": "flat", "conviction": 0.0, "expected_return_pct": 0.0,
            "time_to_target_days": 0, "inputs": {"reason": "no usable returns"},
        }
    mean_r = sum(rets) / len(rets)
    var_r = sum((r - mean_r) ** 2 for r in rets) / len(rets)
    vol = var_r ** 0.5  # daily-ish

    # Mean-reversion thesis: distance from SMA50 normalized by vol.
    z = ((last - sma50) / sma50) / vol if (vol and sma50) else 0.0

    if z < -1.0:
        direction = "long"
        e_return = abs(z) * vol * 100 * 0.6   # expect to recover 60% of the dislocation
    elif z > 1.0:
        direction = "short"
        e_return = abs(z) * vol * 100 * 0.6
    else:
        direction = "flat"
        e_return = 0.0

    time_to_target_days = 21
    conviction = (e_return / time_to_target_days) if time_to_target_days else 0.0

    return {
        "direction": direction,
        "conviction": round(conviction, 4),
        "expected_return_pct": round(e_return if direction == "long" else -e_return, 3),
        "time_to_target_days": time_to_target_days,
        "inputs": {
            "last_close": last,
            "sma50": round(sma50, 2),
            "z_vs_sma50": round(z, 3),
            "daily_vol": round(vol, 4),
        },
    }
