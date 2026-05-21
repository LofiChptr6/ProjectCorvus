"""Single-indicator computation with evidence stamping.

Phase A of CITATION_ARCH (2026-05-21). Where `compute_technicals` returns a
batch dict and makes a Massive API call, `compute_indicator` returns ONE
value, sourced from the local_bars_daily cache, and inserts a row into
evidence_snapshot so the caller can pin a Citation to it.

Design choices:
  - One indicator per call. The replay payload `(symbol, indicator, asof,
    bars_hash)` is small and unambiguous.
  - Source = local_bars_daily, not Massive. Deterministic; matches what
    the bar-streamer caches. Falls back to local_bars (5-min) only if the
    caller explicitly asks for an intraday window.
  - `asof` accepts an ISO date or "today". The harness can replay a
    historical computation by passing the same `asof`.
  - All math is in pure Python — no numpy dep — so this tool is hot-path
    safe (target <10ms inclusive of DB round trip).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Optional

TOOL_VERSION = "0.1.0"

_VALID_INDICATORS = (
    "RSI_14", "RSI_28",
    "SMA_20", "SMA_50", "SMA_200",
    "EMA_9", "EMA_21",
    "ATR_14",
    "BBANDS_20",       # returns {upper, mid, lower}
    "BBAND_POSITION",  # 0.0 = at lower band, 1.0 = at upper band; relative position
    "ABOVE_SMA200",    # boolean: last_close > sma_200
)


def _sma(closes: list[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _ema(closes: list[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    k = 2 / (n + 1)
    ema = sum(closes[:n]) / n
    for c in closes[n:]:
        ema = c * k + ema * (1 - k)
    return ema


def _rsi(closes: list[float], n: int = 14) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[-n:]) / n
    avg_loss = sum(losses[-n:]) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _bbands(closes: list[float], n: int = 20):
    if len(closes) < n:
        return None
    window = closes[-n:]
    mid = sum(window) / n
    var = sum((x - mid) ** 2 for x in window) / n
    std = var ** 0.5
    return {
        "upper": mid + 2 * std,
        "mid": mid,
        "lower": mid - 2 * std,
    }


def _bband_position(closes: list[float], n: int = 20) -> Optional[float]:
    """0.0 = at-or-below lower band, 1.0 = at-or-above upper band, 0.5 = at midline.
    Useful single scalar for the LLM to reason about vs. a 3-tuple."""
    bb = _bbands(closes, n)
    if bb is None:
        return None
    spread = bb["upper"] - bb["lower"]
    if spread <= 0:
        return 0.5
    last = closes[-1]
    pos = (last - bb["lower"]) / spread
    return max(0.0, min(1.0, pos))


def _atr(bars: list[dict], n: int = 14) -> Optional[float]:
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        hl = bars[i]["high"] - bars[i]["low"]
        hc = abs(bars[i]["high"] - bars[i - 1]["close"])
        lc = abs(bars[i]["low"] - bars[i - 1]["close"])
        trs.append(max(hl, hc, lc))
    return sum(trs[-n:]) / n


def _hash_bars_tail(bars: list[dict], n: int = 10) -> str:
    """SHA-256 over the last N bars — proxy for the input dataset state.
    Used as part of the evidence's replay payload."""
    import hashlib
    tail = bars[-n:] if len(bars) >= n else bars
    payload = json.dumps(
        [(b["bar_date"].isoformat() if isinstance(b["bar_date"], date) else str(b["bar_date"]),
          float(b["close"])) for b in tail],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


async def _fetch_daily_bars(symbol: str, asof: Optional[date]) -> list[dict]:
    """Pull daily OHLCV bars from local_bars_daily at-or-before asof.
    Returns dicts with bar_date, open, high, low, close, volume."""
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        if asof is None:
            rows = await conn.fetch(
                """SELECT bar_date, open, high, low, close, volume
                   FROM local_bars_daily WHERE symbol = $1
                   ORDER BY bar_date ASC""",
                symbol.upper(),
            )
        else:
            rows = await conn.fetch(
                """SELECT bar_date, open, high, low, close, volume
                   FROM local_bars_daily WHERE symbol = $1 AND bar_date <= $2
                   ORDER BY bar_date ASC""",
                symbol.upper(), asof,
            )
    return [dict(r) for r in rows]


def _coerce_asof(asof: Optional[str]) -> Optional[date]:
    if not asof or asof == "today":
        return None  # = use latest available
    try:
        return date.fromisoformat(asof)
    except ValueError:
        raise ValueError(f"asof must be ISO date (YYYY-MM-DD) or 'today', got {asof!r}")


async def execute(
    symbol: str,
    indicator: str,
    *,
    asof: Optional[str] = None,
    agent_name: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Compute one indicator on `symbol`. Returns a dict with the value plus
    an evidence_id the caller can attach to a Citation.

    Args:
        symbol: ticker (case-insensitive, uppercased internally).
        indicator: one of `_VALID_INDICATORS`.
        asof: ISO date (YYYY-MM-DD) to compute as-of, or None/"today" for latest.
        agent_name: stamped onto evidence_snapshot for audit.
        session_id: stamped onto evidence_snapshot for cross-reference.

    Returns:
        {
          "ok": True,
          "symbol": "...",
          "indicator": "...",
          "asof": "...",                 # the date the value was computed for
          "value": <scalar or dict>,
          "last_close": <float>,
          "bars_used": <int>,
          "bars_hash": "<short hash>",
          "evidence_id": <int>,
          "computed_at": "<iso>",
        }
        or {"ok": False, "reason": "..."} when computation declines.
    """
    if indicator not in _VALID_INDICATORS:
        return {"ok": False, "reason": f"indicator must be one of {list(_VALID_INDICATORS)}, got {indicator!r}"}
    try:
        asof_date = _coerce_asof(asof)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    bars = await _fetch_daily_bars(symbol, asof_date)
    if not bars:
        return {"ok": False, "reason": f"no local_bars_daily rows for {symbol} (asof={asof or 'today'})"}
    closes = [float(b["close"]) for b in bars]
    last_close = closes[-1]
    actual_asof = bars[-1]["bar_date"]
    # Coerce to date for consistent isoformat
    if isinstance(actual_asof, datetime):
        actual_asof = actual_asof.date()
    bars_hash = _hash_bars_tail(bars)

    value: Any
    if indicator == "RSI_14":
        value = _rsi(closes, 14)
    elif indicator == "RSI_28":
        value = _rsi(closes, 28)
    elif indicator == "SMA_20":
        value = _sma(closes, 20)
    elif indicator == "SMA_50":
        value = _sma(closes, 50)
    elif indicator == "SMA_200":
        value = _sma(closes, 200)
    elif indicator == "EMA_9":
        value = _ema(closes, 9)
    elif indicator == "EMA_21":
        value = _ema(closes, 21)
    elif indicator == "ATR_14":
        value = _atr(bars, 14)
    elif indicator == "BBANDS_20":
        value = _bbands(closes, 20)
    elif indicator == "BBAND_POSITION":
        value = _bband_position(closes, 20)
    elif indicator == "ABOVE_SMA200":
        sma200 = _sma(closes, 200)
        value = (last_close > sma200) if sma200 is not None else None
    else:  # defensive — _VALID_INDICATORS check should have caught
        return {"ok": False, "reason": f"unhandled indicator {indicator!r}"}

    if value is None:
        return {"ok": False, "reason": f"insufficient bars ({len(bars)}) for {indicator}"}

    # Round numeric values for stable storage; dict (BBANDS) gets per-key rounding.
    if isinstance(value, float):
        value = round(value, 4)
    elif isinstance(value, dict):
        value = {k: round(v, 4) if isinstance(v, float) else v for k, v in value.items()}

    asof_iso = actual_asof.isoformat() if isinstance(actual_asof, date) else str(actual_asof)
    computed_at = datetime.now(timezone.utc).isoformat()
    snippet = (
        f"{symbol.upper()} {indicator}={value} (asof {asof_iso}, last_close={round(last_close, 4)}, "
        f"n_bars={len(bars)})"
    )

    from db import store
    evidence_id = await store.stamp_evidence(
        kind="computed_indicator",
        source_ref_id=f"{symbol.upper()}:{indicator}:{asof_iso}",
        inputs_json={
            "symbol": symbol.upper(),
            "indicator": indicator,
            "asof": asof_iso,
            "bars_used": len(bars),
            "bars_hash": bars_hash,
        },
        outputs_json={
            "value": value,
            "last_close": round(last_close, 4),
        },
        content_snippet=snippet,
        computed_by=f"compute_indicator@{TOOL_VERSION}",
        agent_name=agent_name,
        session_id=session_id,
    )

    return {
        "ok": True,
        "symbol": symbol.upper(),
        "indicator": indicator,
        "asof": asof_iso,
        "value": value,
        "last_close": round(last_close, 4),
        "bars_used": len(bars),
        "bars_hash": bars_hash,
        "evidence_id": evidence_id,
        "computed_at": computed_at,
    }
