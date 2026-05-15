"""Vera iv_crush_setup: 5-bar range expansion gated by VIXY-vs-mean + XLV RSI.

Cross-asset extras:
- VIXY: 1x VIXY ETF as VIX-proxy (spot VIX is index-only, not fetchable).
  Used as a *relative* signal: current close / 60-day mean. >1.1 means the
  vol environment is meaningfully above its recent average. Absolute VIXY
  level can't be compared to the historical 18 VIX threshold because VIXY
  embeds futures contango/roll costs.
- XLV: Healthcare sector ETF. RSI_14 used as sector momentum gate.
"""
from __future__ import annotations

from typing import Any

MODEL_VERSION = "1.2"
BAR_FREQUENCY = "1d"
MIN_BARS = 25
LOOKBACK_DAYS = 60
EXTRA_SYMBOLS = ["VIXY", "XLV"]


def _rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    avg_g = gains / n
    avg_l = losses / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 2)


def _avg_range(bars: list[dict]) -> float:
    rs = [b["h"] - b["l"] for b in bars if b["h"] is not None and b["l"] is not None]
    return sum(rs) / len(rs) if rs else 0.0


def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if len(bars) < MIN_BARS:
        return {
            "signal": None, "direction": None, "conviction": 0.0,
            "expected_return_pct": 0.0, "time_to_target_days": 0,
            "inputs": {}, "reason": f"need >={MIN_BARS} bars, got {len(bars)}",
        }

    extras = context.get("extra_bars") or {}
    vix_bars = extras.get("VIXY") or []
    xlv_bars = extras.get("XLV") or []
    if len(vix_bars) < 30 or len(xlv_bars) < 15:
        return {
            "signal": None, "direction": None, "conviction": 0.0,
            "expected_return_pct": 0.0, "time_to_target_days": 0,
            "inputs": {},
            "reason": f"extras incomplete (VIXY={len(vix_bars)}, XLV={len(xlv_bars)}; need VIXY>=30 and XLV>=15)",
        }

    r5 = _avg_range(bars[-5:])
    r20 = _avg_range(bars[-25:-5])
    expansion = r5 / r20 if r20 else 0.0

    vix_closes = [float(b["c"]) for b in vix_bars]
    vix_now = vix_closes[-1]
    vix_60d_mean = sum(vix_closes[-60:]) / min(60, len(vix_closes))
    vix_ratio = vix_now / vix_60d_mean if vix_60d_mean else 0.0
    xlv_rsi = _rsi([b["c"] for b in xlv_bars])
    if xlv_rsi is None:
        return {
            "signal": None, "direction": None, "conviction": 0.0,
            "expected_return_pct": 0.0, "time_to_target_days": 0,
            "inputs": {}, "reason": "xlv_rsi computation failed",
        }

    inputs = {
        "expansion_ratio": round(expansion, 3),
        "vixy_ratio_vs_60d": round(vix_ratio, 3),
        "xlv_rsi": float(xlv_rsi),
    }

    if expansion > 1.5 and vix_ratio > 1.1:
        # Vol expansion + options regime rich → bearish-fade setup. Per the
        # desk's inverse-ETF convention the model emits 'flat'; the LLM
        # decides whether to express it via an inverse-ETF long.
        return {
            "signal": round(expansion, 3),
            "direction": "flat",
            "conviction": 0.0,
            "expected_return_pct": 0.0,
            "time_to_target_days": 3,
            "inputs": inputs,
            "interpretation": "vol expansion + VIXY above 60d mean (fade-IV; bearish — emit flat)",
        }
    if expansion < 0.7 and xlv_rsi < 40:
        return {
            "signal": round(-expansion, 3),
            "direction": "long",
            "conviction": round(min(0.4 + (40 - xlv_rsi) / 100, 0.9), 3),
            "expected_return_pct": 4.0,
            "time_to_target_days": 5,
            "inputs": inputs,
            "interpretation": "vol contraction + XLV oversold (IV cheap; lean directional long)",
        }
    return {
        "signal": round(expansion, 3),
        "direction": "flat",
        "conviction": 0.0,
        "expected_return_pct": 0.0,
        "time_to_target_days": 5,
        "inputs": inputs,
        "interpretation": "no IV setup",
    }
