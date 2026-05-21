"""Is the symbol trading above its 200-day SMA? Boolean trend-regime signal.

Thin wrapper around the compute_indicator tool that asks one specific
question: "is `last_close > SMA_200`?" Useful for trend-following gates
("only go long names above the 200DMA").

Args:
    symbol: ticker (case-insensitive)
    asof: optional ISO date for replay; defaults to latest

Returns:
    {ok, result: bool, inputs_used, evidence_id} on success.
    {ok: False, reason: str} when insufficient bars or symbol missing.
"""
from __future__ import annotations

from typing import Any, Optional

SKILL_VERSION = "0.1.0"
SKILL_DESCRIPTION = "Is `symbol` trading above its 200-day SMA? Returns bool with evidence_id."


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
        symbol=symbol, indicator="ABOVE_SMA200", asof=asof,
        agent_name=agent_name, session_id=session_id,
    )
    if not res.get("ok"):
        return {"ok": False, "reason": res.get("reason") or "compute_indicator declined"}
    return {
        "ok": True,
        "result": bool(res["value"]),
        "inputs_used": {"symbol": symbol.upper(), "asof": res["asof"]},
        "evidence_id": res["evidence_id"],
        "last_close": res["last_close"],
    }
