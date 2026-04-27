"""Atlas bootstrap indicator: regime_score.

Composite long-side regime score in [-1, 1]:
  +0.5 if last close > 200d MA (long-term trend)
  +0.3 if 50d MA > 200d MA (golden-cross structure)
  +0.2 if 20d MA slope is positive (recent acceleration)

Pass at least 200 daily bars for a real reading.
"""

from __future__ import annotations

from typing import Any


def _sma(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if len(bars) < 50:
        return {"signal": None, "reason": f"need >=50 bars, got {len(bars)}"}

    closes = [b["c"] for b in bars]
    last_close = closes[-1]
    sma200 = _sma(closes, 200)
    sma50 = _sma(closes, 50)
    sma20_now = _sma(closes, 20)
    sma20_prev = _sma(closes[:-5], 20) if len(closes) > 25 else None

    score = 0.0
    parts: dict[str, Any] = {}

    if sma200 is not None:
        above_200 = last_close > sma200
        score += 0.5 if above_200 else -0.5
        parts["above_200d_ma"] = above_200
        parts["sma200"] = round(sma200, 2)
    if sma50 is not None and sma200 is not None:
        golden = sma50 > sma200
        score += 0.3 if golden else -0.3
        parts["golden_cross"] = golden
        parts["sma50"] = round(sma50, 2)
    if sma20_now is not None and sma20_prev is not None:
        slope_up = sma20_now > sma20_prev
        score += 0.2 if slope_up else -0.2
        parts["sma20_slope_up"] = slope_up

    direction = "long" if score > 0.3 else "short" if score < -0.3 else "flat"
    e_return = round(score * 6.0, 3) if direction != "flat" else 0.0
    horizon = 60
    conviction = round(abs(e_return) / horizon, 4) if horizon else 0.0

    return {
        "signal": round(score, 3),
        "last_close": last_close,
        "components": parts,
        "regime_from_mike": context.get("regime"),
        "interpretation": (
            "long-friendly" if score > 0.5
            else "short-friendly" if score < -0.5
            else "mixed / chop"
        ),
        "direction": direction,
        "conviction": conviction,
        "expected_return_pct": e_return,
        "time_to_target_days": horizon,
        "inputs": {
            "score": round(score, 3),
            "above_sma200": parts.get("above_200d_ma"),
            "golden_cross": parts.get("golden_cross"),
            "sma20_slope_pos": parts.get("sma20_slope_up"),
        },
    }
