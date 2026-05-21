"""14-day Average True Range, in price units. Used for stop-loss sizing.

Wraps compute_indicator(symbol, "ATR_14"). The ATR is a volatility proxy — a
$2 ATR on a $100 stock implies typical 2% daily ranges, which informs stop
distances (e.g. stop_pct = 1.5 × ATR / price → drawdown beyond ~1.5 daily
ranges flips the position to flat).

Args:
    symbol: ticker (case-insensitive)
    asof: optional ISO date for replay; defaults to latest

Returns:
    {ok, result: {atr, last_close, stop_pct_1_5x}, inputs_used, evidence_id}
    The convenience `stop_pct_1_5x` field is a suggested stop distance as a
    percent magnitude — caller still owns the sizing decision.
"""
from __future__ import annotations

from typing import Any, Optional

SKILL_VERSION = "0.1.0"
SKILL_DESCRIPTION = "14-day ATR with a suggested stop_pct (1.5× ATR / last_close)."


async def compute(
    symbol: str,
    *,
    asof: Optional[str] = None,
    agent_name: Optional[str] = None,
    session_id: Optional[str] = None,
    **_unused: Any,
) -> dict[str, Any]:
    if not symbol:
        return {"ok": False, "reason": "symbol is required"}
    from tools.analysis.compute_indicator import execute as compute_indicator
    res = await compute_indicator(
        symbol=symbol, indicator="ATR_14", asof=asof,
        agent_name=agent_name, session_id=session_id,
    )
    if not res.get("ok"):
        return {"ok": False, "reason": res.get("reason") or "compute_indicator declined"}
    atr = float(res["value"])
    last_close = float(res["last_close"])
    stop_pct = round(1.5 * atr / last_close * 100, 2) if last_close > 0 else None
    return {
        "ok": True,
        "result": {
            "atr": atr,
            "last_close": last_close,
            "stop_pct_1_5x": stop_pct,
        },
        "inputs_used": {"symbol": symbol.upper(), "asof": res["asof"]},
        "evidence_id": res["evidence_id"],
    }
