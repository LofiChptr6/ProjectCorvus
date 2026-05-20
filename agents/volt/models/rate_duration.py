from __future__ import annotations
from typing import Any

import numpy as np
import pandas as pd
from datetime import datetime, timedelta

def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if len(bars) < 60:
        return {
            "direction": "flat",
            "likelihood": 0.0,
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
            "direction": "flat",
            "likelihood": 0.0,
            "expected_return_pct": 0.0,
            "time_to_target_days": 0,
            "inputs": {"reason": "no usable returns"},
        }
    mean_r = np.mean(rets)
    var_r = np.var(rets)
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
    likelihood = (e_return / time_to_target_days) if time_to_target_days else 0.0

    return {
        "direction": direction,
        "likelihood": round(likelihood, 4),
        "expected_return_pct": round(e_return if direction == "long" else -e_return, 3),
        "time_to_target_days": time_to_target_days,
        "inputs": {
            "last_close": last,
            "sma50": round(sma50, 2),
            "z_vs_sma50": round(z, 3),
            "daily_vol": round(vol, 4),
        },
    }

MODEL_VERSION = "1.2"