"""Is there a recent news catalyst for `symbol` matching any of `terms`?

Wraps query_news with a recency window tuned for catalyst hunting (default
3 days) and AND-semantics on the symbol mention. Returns the top hits plus
a confidence tier so the LLM can decide whether the catalyst is strong
enough to cite in a rationale.

Args:
    symbol: ticker (case-insensitive)
    terms: list of catalyst terms (e.g. ["earnings", "guidance"], ["FDA approval"])
    window_days: how far back to look (default 3)

Returns:
    {ok, result: {found, n_matches, confidence, top_match_ids},
     inputs_used, evidence_id}

    Confidence tiers:
      - 'strong': ≥3 matches in window — sustained coverage
      - 'mention': 1-2 matches in window — single news item
      - 'absent': 0 matches — no catalyst in window
"""
from __future__ import annotations

from typing import Any, Optional

SKILL_VERSION = "0.1.0"
SKILL_DESCRIPTION = "Search news for a symbol+terms catalyst; returns confidence tier + evidence_id."


async def compute(
    symbol: str,
    *,
    terms: list[str],
    window_days: int = 3,
    agent_name: Optional[str] = None,
    session_id: Optional[str] = None,
    **_unused: Any,
) -> dict[str, Any]:
    if not symbol:
        return {"ok": False, "reason": "symbol is required"}
    if not isinstance(terms, list) or not terms:
        return {"ok": False, "reason": "terms must be a non-empty list"}
    from tools.analysis.query_news import execute as query_news
    res = await query_news(
        terms=terms, symbol=symbol, window_days=window_days,
        agent_name=agent_name, session_id=session_id, limit=20,
    )
    if not res.get("ok"):
        return {"ok": False, "reason": res.get("reason") or "query_news declined"}
    n = res["match_count"]
    confidence = "strong" if n >= 3 else ("mention" if n >= 1 else "absent")
    top_ids = [m["post_id"] for m in res["matches"][:5]]
    return {
        "ok": True,
        "result": {
            "found": n > 0,
            "n_matches": n,
            "confidence": confidence,
            "top_match_ids": top_ids,
        },
        "inputs_used": {
            "symbol": symbol.upper(),
            "terms": terms,
            "window_days": window_days,
        },
        "evidence_id": res["evidence_id"],
    }
