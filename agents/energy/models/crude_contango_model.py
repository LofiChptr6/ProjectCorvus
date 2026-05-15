"""Energy crude_contango_model: backwardation/contango regime via USO vs USL.

USO ~ front-month crude; USL ~ 12-month-average crude. USO outperforming USL
over a 30-bar window proxies a backwardated curve (tight supply, bullish energy).
USL outperforming proxies contango (oversupply, bearish energy → emit flat).
"""
from __future__ import annotations

from typing import Any

MODEL_VERSION = "1.1"
BAR_FREQUENCY = "1d"
MIN_BARS = 2
LOOKBACK_DAYS = 60
EXTRA_SYMBOLS = ["USO", "USL"]


def _ret_n(bars: list[dict], n: int) -> float | None:
    if len(bars) < n + 1:
        return None
    base = float(bars[-n - 1]["c"])
    last = float(bars[-1]["c"])
    if base == 0:
        return None
    return (last - base) / base


def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    extras = context.get("extra_bars") or {}
    uso_bars = extras.get("USO") or []
    usl_bars = extras.get("USL") or []
    if len(uso_bars) < 31 or len(usl_bars) < 31:
        return {
            "signal": None, "direction": None, "conviction": 0.0,
            "expected_return_pct": 0.0, "time_to_target_days": 0,
            "inputs": {},
            "reason": f"need >=31 bars USO+USL, got USO={len(uso_bars)} USL={len(usl_bars)}",
        }

    uso_30 = _ret_n(uso_bars, 30)
    usl_30 = _ret_n(usl_bars, 30)
    if uso_30 is None or usl_30 is None:
        return {
            "signal": None, "direction": None, "conviction": 0.0,
            "expected_return_pct": 0.0, "time_to_target_days": 0,
            "inputs": {}, "reason": "30-day return compute failed",
        }
    spread = uso_30 - usl_30

    inputs = {
        "uso_30d_return_pct": round(uso_30 * 100, 3),
        "usl_30d_return_pct": round(usl_30 * 100, 3),
        "uso_minus_usl_pct": round(spread * 100, 3),
    }

    # Threshold ~1% spread over 30 days.
    if spread > 0.01:
        return {
            "signal": round(spread * 100, 3),
            "direction": "long",
            "conviction": round(min(abs(spread) * 30, 0.8), 3),
            "expected_return_pct": round(min(abs(spread) * 100, 4.0), 3),
            "time_to_target_days": 14,
            "inputs": inputs,
            "interpretation": "backwardation (USO > USL 30d) — bullish energy",
        }
    if spread < -0.01:
        return {
            "signal": round(spread * 100, 3),
            "direction": "flat",
            "conviction": 0.0,
            "expected_return_pct": 0.0,
            "time_to_target_days": 14,
            "inputs": inputs,
            "interpretation": "contango (USL > USO 30d) — bearish energy; emit flat",
        }
    return {
        "signal": round(spread * 100, 3),
        "direction": "flat",
        "conviction": 0.0,
        "expected_return_pct": 0.0,
        "time_to_target_days": 14,
        "inputs": inputs,
        "interpretation": "neutral curve regime",
    }
