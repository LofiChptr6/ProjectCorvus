"""Commodity gold_real_yield_model: gold direction via TIP ETF as real-yield proxy.

TIP tracks 10Y TIPS; TIP daily return inversely tracks real-yield change.
TIP down → TIPS yields up → gold bearish (emit flat per inverse-ETF route).
TIP up → real yields easing → gold long.
"""
from __future__ import annotations

from typing import Any

MODEL_VERSION = "1.1"
BAR_FREQUENCY = "1d"
MIN_BARS = 2
LOOKBACK_DAYS = 30
EXTRA_SYMBOLS = ["TIP"]


def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if len(bars) < MIN_BARS:
        return {
            "signal": None, "direction": None, "likelihood": 0.0,
            "expected_return_pct": 0.0, "time_to_target_days": 0,
            "inputs": {}, "reason": f"need >={MIN_BARS} bars, got {len(bars)}",
        }
    tip_bars = (context.get("extra_bars") or {}).get("TIP") or []
    if len(tip_bars) < 2:
        return {
            "signal": None, "direction": None, "likelihood": 0.0,
            "expected_return_pct": 0.0, "time_to_target_days": 0,
            "inputs": {}, "reason": f"extra_bars.TIP needs >=2, got {len(tip_bars)}",
        }

    tip_prev = float(tip_bars[-2]["c"])
    tip_now = float(tip_bars[-1]["c"])
    tip_return_pct = (tip_now - tip_prev) / tip_prev * 100 if tip_prev else 0.0
    real_yield_change_proxy_bps = round(-tip_return_pct * 100, 2)  # inverse, in bps

    inputs = {
        "tip_return_pct": round(tip_return_pct, 3),
        "real_yield_change_proxy_bps": real_yield_change_proxy_bps,
    }

    # Threshold: ~0.3% TIP move on the day (≈ 30 bps yield change).
    if tip_return_pct < -0.3:
        # TIP down → real yields up → gold bearish → flat (inverse-ETF route).
        return {
            "signal": round(real_yield_change_proxy_bps, 3),
            "direction": "flat",
            "likelihood": 0.0,
            "expected_return_pct": 0.0,
            "time_to_target_days": 5,
            "inputs": inputs,
            "interpretation": "real yields rising (TIP down) — bearish gold; emit flat",
        }
    if tip_return_pct > 0.3:
        return {
            "signal": round(real_yield_change_proxy_bps, 3),
            "direction": "long",
            "likelihood": round(min(abs(tip_return_pct) / 1.0, 0.7), 3),
            "expected_return_pct": round(min(abs(tip_return_pct) * 5, 3.0), 3),
            "time_to_target_days": 10,
            "inputs": inputs,
            "interpretation": "real yields easing (TIP up) — bullish gold",
        }
    return {
        "signal": round(real_yield_change_proxy_bps, 3),
        "direction": "flat",
        "likelihood": 0.0,
        "expected_return_pct": 0.0,
        "time_to_target_days": 10,
        "inputs": inputs,
        "interpretation": "no decisive real-yield move",
    }
