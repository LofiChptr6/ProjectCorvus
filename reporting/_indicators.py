"""Numpy-only technical-indicator helpers used by the evening slide
forecast-panel renderer. Inputs are 1D arrays of close prices in time
order; outputs are arrays of the same length with NaN-padding at the
front for the warm-up window.

Kept intentionally tiny so it has no extra dependencies beyond numpy.
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np


def sma(close: Sequence[float], period: int) -> np.ndarray:
    """Simple moving average. Output[i] = mean(close[i-period+1:i+1]) for
    i >= period-1, NaN earlier."""
    arr = np.asarray(close, dtype=float)
    n = arr.size
    out = np.full(n, np.nan, dtype=float)
    if period <= 0 or period > n:
        return out
    cum = np.cumsum(np.insert(arr, 0, 0.0))
    # cum[k] = sum(arr[:k]); window sum at index i = cum[i+1] - cum[i+1-period]
    out[period - 1:] = (cum[period:] - cum[:-period]) / float(period)
    return out


def rsi(close: Sequence[float], period: int = 14) -> np.ndarray:
    """Wilder-smoothed RSI. Returns 0..100 with NaN for the warm-up.

    Uses the EMA-style smoothing alpha = 1/period (Wilder's original)
    rather than a simple rolling mean of gains/losses."""
    arr = np.asarray(close, dtype=float)
    n = arr.size
    out = np.full(n, np.nan, dtype=float)
    if n <= period:
        return out
    delta = np.diff(arr)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = float(gain[:period].mean())
    avg_loss = float(loss[:period].mean())
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gain[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i - 1]) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def bbands(close: Sequence[float], period: int = 20, k: float = 2.0
           ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Bollinger bands (upper, middle, lower). Middle = SMA(period); upper
    and lower are middle ± k × population stdev over the same window. NaN
    for the warm-up window. Uses numpy stride tricks for the rolling stdev
    so this stays O(n)."""
    arr = np.asarray(close, dtype=float)
    n = arr.size
    upper = np.full(n, np.nan)
    middle = sma(arr, period)
    lower = np.full(n, np.nan)
    if period <= 0 or period > n:
        return upper, middle, lower
    # Rolling std via cumulative-sum identity: var = E[x²] - (E[x])²
    cum_sq = np.cumsum(np.insert(arr * arr, 0, 0.0))
    mean_sq = (cum_sq[period:] - cum_sq[:-period]) / float(period)
    var = mean_sq - (middle[period - 1:] ** 2)
    var = np.maximum(var, 0.0)  # numerical safety
    std = np.sqrt(var)
    upper[period - 1:] = middle[period - 1:] + k * std
    lower[period - 1:] = middle[period - 1:] - k * std
    return upper, middle, lower


def parse_indicator_name(name: str) -> tuple[str, int]:
    """`SMA_50` → ("SMA", 50). Robust to lowercase / extra whitespace."""
    s = (name or "").strip().upper().replace(" ", "")
    if "_" in s:
        kind, period = s.split("_", 1)
        try:
            return kind, int(period)
        except ValueError:
            return kind, 0
    return s, 0


def indicator_summary(close: Sequence[float], spec: dict) -> dict:
    """Compact human-readable snapshot at the latest bar for use in the
    per-row title. Returns a dict with the keys that exist (RSI, distance
    to each trend SMA, BBANDS-relative position)."""
    arr = np.asarray(close, dtype=float)
    if arr.size == 0:
        return {}
    last = float(arr[-1])
    out: dict[str, str] = {}

    osc_name = (spec.get("oscillator") or "").upper()
    if osc_name.startswith("RSI"):
        _, period = parse_indicator_name(osc_name)
        if period <= 0:
            period = 14
        rsi_series = rsi(arr, period=period)
        if not math.isnan(rsi_series[-1]):
            r = float(rsi_series[-1])
            out["rsi"] = f"RSI={r:.1f}"
            ob = spec.get("overbought") or 70
            os_ = spec.get("oversold") or 30
            if r >= ob:
                out["rsi_state"] = "overbought"
            elif r <= os_:
                out["rsi_state"] = "oversold"
            else:
                out["rsi_state"] = "neutral"

    for trend in spec.get("trend") or []:
        kind, period = parse_indicator_name(trend)
        if kind != "SMA" or period <= 0:
            continue
        s = sma(arr, period)
        if math.isnan(s[-1]):
            continue
        rel = (last / float(s[-1]) - 1.0) * 100.0
        out[f"px_vs_sma{period}"] = f"px {('+' if rel >= 0 else '')}{rel:.1f}% vs SMA{period}"

    env = (spec.get("envelope") or "").upper()
    if env.startswith("BBAND"):
        _, period = parse_indicator_name(env)
        if period <= 0:
            period = 20
        upper, middle, lower = bbands(arr, period=period)
        if not math.isnan(upper[-1]):
            up = float(upper[-1])
            lo = float(lower[-1])
            mid = float(middle[-1])
            if last >= up:
                out["bb_state"] = "@upper-band"
            elif last <= lo:
                out["bb_state"] = "@lower-band"
            else:
                # Position in band as a percentage (0=lower, 100=upper)
                width = up - lo
                pos = ((last - lo) / width * 100.0) if width > 0 else 50.0
                out["bb_state"] = f"BB-pos {pos:.0f}%"

    return out
