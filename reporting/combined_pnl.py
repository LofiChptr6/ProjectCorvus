"""Combined (realized + unrealized) P&L computation.

Crosses three sources:
  - `agent_pnl_attribution` (per-agent realized — closed positions only)
  - IBKR `get_account_summary()` (desk-level realized + unrealized, IBKR-canonical)
  - IBKR `get_positions()` (per-symbol mark-to-market unrealized)
  - `agent_conviction` active rows (attribution weights for unrealized)

Used by every human/LLM-facing P&L surface so realized-only blindness goes away.

Per-agent unrealized attribution (Rule B): for each open position with non-zero
unrealized P&L, take the active conviction rows on that symbol whose direction
matches the position direction, normalize their |conviction| weights, and split
the position's unrealized P&L by those weights. Open exposure with no matching
conviction is bucketed as `__orphan__` so the per-agent rows always reconcile
back to the desk total.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


async def _get_open_attribution_shares(symbol: str) -> dict[str, float]:
    """Return {agent_name: sum(attribution_share)} for open (unsettled) fills on symbol.

    Uses the stored attribution_shares from agent_pnl_attribution rather than
    live conviction weights, so it works correctly after convictions expire at EOD.
    Shares are raw sums across multiple decisions — caller normalises.
    """
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT agent_name, SUM(attribution_share)::float8 AS total_share
               FROM agent_pnl_attribution
               WHERE symbol = $1 AND attributed_pnl IS NULL
               GROUP BY agent_name""",
            symbol.upper(),
        )
    return {r["agent_name"]: float(r["total_share"]) for r in rows if r["total_share"]}


async def get_pnl_combined(agent_name: Optional[str] = None) -> dict:
    """Single source of truth for "right-now" P&L across the desk.

    Returns:
        {
          "rows": [
            {"agent_name": str, "realized_pnl": float, "unrealized_pnl": float,
             "total_pnl": float, "num_fills": int, "trade_date": "YYYY-MM-DD"},
            ...
          ],
          "desk": {
            "realized_total": float,            # IBKR-canonical (incl. commissions)
            "unrealized_total": float,          # IBKR-canonical
            "combined_total": float,
            "attribution_realized_sum": float,  # sum across agents from attribution table
            "commission_gap": float,            # realized_total - attribution_realized_sum
            "orphan_unrealized": float,
          },
        }

    For multi-period queries (week/month) where IBKR doesn't keep a snapshot,
    callers should use `db.store.get_pnl_summary()` directly — combined only
    makes sense for the current session.
    """
    import db.store as store
    from ibkr.account import get_account_summary, get_positions

    # 1. Per-agent realized today from the attribution table.
    realized_rows = await store.get_pnl_summary(agent_name=agent_name, period="today")
    by_agent_realized: dict[str, dict] = {}
    for r in realized_rows:
        by_agent_realized[r["agent_name"]] = {
            "agent_name": r["agent_name"],
            "trade_date": r.get("trade_date"),
            "realized_pnl": float(r.get("total_pnl") or 0.0),
            "num_fills": int(r.get("num_fills") or 0),
        }

    # 2. Desk-level numbers from IBKR (canonical truth incl. commissions).
    try:
        summary = await get_account_summary()
        desk_realized = float(summary.get("realized_pnl_today") or 0.0)
        desk_unrealized = float(summary.get("unrealized_pnl") or 0.0)
    except Exception as exc:
        log.warning("get_pnl_combined: account summary unavailable (%s)", exc)
        desk_realized = 0.0
        desk_unrealized = 0.0

    # 3. Per-position unrealized (split by Rule B: active conviction weights).
    try:
        positions = await get_positions()
    except Exception as exc:
        log.warning("get_pnl_combined: positions unavailable (%s)", exc)
        positions = []

    by_agent_unrealized: dict[str, float] = {}
    orphan_unrealized = 0.0

    for pos in positions:
        sym = pos.get("symbol")
        sym_unreal = float(pos.get("unrealized_pnl") or 0.0)
        if not sym or sym_unreal == 0:
            continue

        # Primary: use stored attribution_shares from open (unsettled) fills.
        # These survive conviction expiry and reflect actual trade weights.
        try:
            attr_rows = await _get_open_attribution_shares(sym)
        except Exception:
            attr_rows = {}

        if attr_rows:
            total_w = sum(attr_rows.values())
            if total_w > 0:
                for agent, w in attr_rows.items():
                    by_agent_unrealized[agent] = (
                        by_agent_unrealized.get(agent, 0.0) + sym_unreal * w / total_w
                    )
                continue

        # Fallback: active conviction weights (works intraday when attribution
        # rows haven't been written yet for a very fresh fill).
        try:
            convictions = await store.get_convictions_for_symbol(sym)
        except Exception:
            convictions = []

        qty = float(pos.get("quantity") or 0.0)
        position_dir = "long" if qty > 0 else "short" if qty < 0 else None
        matching = [c for c in convictions
                    if c.get("direction") == position_dir
                    and abs(float(c.get("conviction") or 0)) > 0] if position_dir else []
        total_w = sum(abs(float(c["conviction"])) for c in matching)
        if total_w <= 0:
            orphan_unrealized += sym_unreal
            continue
        for c in matching:
            w = abs(float(c["conviction"])) / total_w
            a = c["agent_name"]
            by_agent_unrealized[a] = by_agent_unrealized.get(a, 0.0) + sym_unreal * w

    # 4. Merge realized + unrealized into combined rows.
    all_agents = set(by_agent_realized) | set(by_agent_unrealized)
    if agent_name:
        all_agents = {a for a in all_agents if a == agent_name}
    rows: list[dict] = []
    for a in sorted(all_agents):
        base = by_agent_realized.get(a, {"agent_name": a, "trade_date": None,
                                          "realized_pnl": 0.0, "num_fills": 0})
        unreal = by_agent_unrealized.get(a, 0.0)
        rows.append({
            "agent_name": a,
            "trade_date": base.get("trade_date"),
            "realized_pnl": base["realized_pnl"],
            "unrealized_pnl": unreal,
            "total_pnl": base["realized_pnl"] + unreal,
            "num_fills": base["num_fills"],
        })
    rows.sort(key=lambda r: r["total_pnl"], reverse=True)

    attribution_realized_sum = sum(r["realized_pnl"] for r in rows)
    return {
        "rows": rows,
        "desk": {
            "realized_total": desk_realized,
            "unrealized_total": desk_unrealized,
            "combined_total": desk_realized + desk_unrealized,
            "attribution_realized_sum": attribution_realized_sum,
            "commission_gap": desk_realized - attribution_realized_sum,
            "orphan_unrealized": orphan_unrealized,
        },
    }


async def get_symbol_unrealized(symbol: str) -> float:
    """Current mark-to-market unrealized P&L for one symbol; 0 if not held."""
    from ibkr.account import get_positions
    sym = symbol.upper()
    try:
        positions = await get_positions()
    except Exception as exc:
        log.warning("get_symbol_unrealized(%s): positions fetch failed (%s)", symbol, exc)
        return 0.0
    for p in positions:
        if (p.get("symbol") or "").upper() == sym:
            return float(p.get("unrealized_pnl") or 0.0)
    return 0.0
