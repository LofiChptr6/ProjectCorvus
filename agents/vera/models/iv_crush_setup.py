"""Vera bootstrap indicator: iv_crush_setup.

True IV requires options-chain data which the desk doesn't have yet (Vera should
raise_tool_gap for this). As a stand-in, this measures realized-vol expansion
into the close: if the last 5 bars' range is wide vs the 20-bar average, the
stock is 'priced for movement' — a proxy for elevated implied vol.
"""

from __future__ import annotations

from typing import Any


def _avg_range(bars: list[dict]) -> float:
    ranges = [(b["h"] - b["l"]) for b in bars if b["h"] is not None and b["l"] is not None]
    return sum(ranges) / len(ranges) if ranges else 0.0


def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if len(bars) < 25:
        return {"signal": None, "reason": f"need >=25 bars, got {len(bars)}"}

    recent5 = bars[-5:]
    base20 = bars[-25:-5]
    r5 = _avg_range(recent5)
    r20 = _avg_range(base20)
    expansion = (r5 / r20) if r20 else 0.0

    return {
        "signal": round(expansion, 3),
        "avg_range_last_5": round(r5, 3),
        "avg_range_prior_20": round(r20, 3),
        "interpretation": (
            "vol expansion — options likely rich, fade IV after report" if expansion > 1.5
            else "vol contraction — IV likely cheap, lean directional" if expansion < 0.7
            else "no edge from realized vol"
        ),
        "note": "True IV crush needs options chain data — raise_tool_gap('get_options_chain', ...) if not present.",
        "direction": "flat",
        "conviction": 0.0,
        "expected_return_pct": 0.0,
        "time_to_target_days": 5,
        "inputs": {
            "range_5": round(r5, 3),
            "range_20_avg": round(r20, 3),
            "expansion_ratio": round(expansion, 3),
        },
    }
