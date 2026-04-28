"""Holding Kanban — write per-(agent, symbol) snapshot rows after every
rebalance_desk decision.

Triggered from `mcp_server.rebalance_desk` immediately after the
`allocation_decision` row is recorded (live or dry-run). For each currently
held symbol we attribute the position to contributing agents using the
same precedence as combined_pnl.py:

  1. Primary  — sum stored attribution_share rows from agent_pnl_attribution
                 where attributed_pnl IS NULL (open fills). This survives
                 conviction TTL expiry and reflects who actually drove the
                 trades that built the current position.
  2. Fallback — normalized |conviction| from the decision's contributing_views
                 for that symbol. Used when fills haven't been written yet.

Cash is attributed across `cash_contributors` (already weighted by conviction
inside compute_target_weights) — normalized to 1.0 and applied to the
current cash balance (desk_nav × cash_weight). Each contributing agent gets
one CASH row.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


async def _attribution_shares(symbol: str) -> dict[str, float]:
    """{agent: total_share} for open fills on symbol — copied from
    reporting.combined_pnl._get_open_attribution_shares so we don't take a
    runtime dep on the reporting package."""
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT agent_name, SUM(attribution_share)::float8 AS total_share
               FROM agent_pnl_attribution
               WHERE symbol=$1 AND attributed_pnl IS NULL
               GROUP BY agent_name""",
            symbol.upper(),
        )
    return {r["agent_name"]: float(r["total_share"]) for r in rows if r["total_share"]}


async def snapshot_after_decision(
    decision_id: int,
    nav: float,
    target_weights: dict,
    contributing_views: dict,
    cash_weight: float,
    cash_contributors: list,
) -> dict:
    """Build and insert holding_kanban rows for the just-recorded decision.

    Args:
        decision_id: id of the allocation_decision row we're snapshotting against.
        nav: desk NAV at decision time.
        target_weights: {symbol: weight} from compute_target_weights.
        contributing_views: {symbol: [{agent, weight}, ...]} from the decision row.
        cash_weight: target cash fraction of NAV.
        cash_contributors: [{agent, weight}, ...] (or list of (agent, weight) tuples).

    Returns:
        {"rows_inserted": int, "agents": int, "symbols": int}
    """
    from db import store
    from ibkr.account import get_positions

    # 1. Live positions (qty + market_value + market_price).
    try:
        positions = await get_positions()
    except Exception as exc:
        log.warning("snapshot_after_decision: positions fetch failed (%s)", exc)
        positions = []

    # 2. Build per-symbol agent shares + per-agent rows for held symbols.
    rows: list[dict] = []
    agents_seen: set[str] = set()

    for pos in positions:
        sym = (pos.get("symbol") or "").upper()
        qty = float(pos.get("quantity") or 0.0)
        if not sym or qty == 0:
            continue
        price = float(pos.get("market_price") or 0.0)
        # IBKR sometimes returns 0 marketPrice for stale snapshots; fall back
        # to market_value/qty so the row is still meaningful.
        if price <= 0 and pos.get("market_value"):
            try:
                price = float(pos["market_value"]) / qty
            except Exception:
                price = 0.0

        # Primary: attribution shares from open fills.
        attr = await _attribution_shares(sym)
        # Fallback: conviction weights from the decision (only for the matching
        # direction — diff_to_orders translates negative weights into long
        # inverse-ETF positions, so we lookup by held symbol AND by any
        # original symbol whose inverse resolves here).
        if not attr:
            contribs = contributing_views.get(sym, []) or contributing_views.get(sym.upper(), [])
            if not contribs:
                # The held symbol may be an inverse ETF; scan contributing_views
                # for the original symbol that resolved to this one.
                try:
                    from meta_agent.allocator import resolve_bearish_vehicle
                    sector_map = _load_sector_map_safe()
                    for orig, views in contributing_views.items():
                        if not views:
                            continue
                        try:
                            v, mode = resolve_bearish_vehicle(orig, sector_map)
                        except Exception:
                            continue
                        if mode == "inverse_etf" and v.upper() == sym:
                            contribs = views
                            break
                except Exception:
                    contribs = []
            if contribs:
                total = sum(abs(float(c.get("weight", 0))) for c in contribs)
                if total > 0:
                    attr = {
                        c["agent"]: abs(float(c["weight"])) / total
                        for c in contribs if abs(float(c.get("weight", 0))) > 0
                    }

        if not attr:
            # Orphan position — no attribution. Record under '__orphan__'
            # so the trajectory remains complete.
            rows.append({
                "agent_name": "__orphan__",
                "symbol": sym,
                "holding_qty": qty,
                "attribution_share": 1.0,
                "conviction": None,
                "direction": "long" if qty > 0 else "short",
                "price_per_share": price,
                "market_value": qty * price,
            })
            agents_seen.add("__orphan__")
            continue

        # Normalize and attribute.
        total_share = sum(attr.values())
        if total_share <= 0:
            continue
        # Pull conviction/direction lookup from the contributing_views for this
        # symbol so each row carries what the agent actually published (or NULL
        # if they're inheriting an old position).
        conv_lookup: dict[str, dict] = {}
        for c in contributing_views.get(sym, []) or []:
            conv_lookup[c["agent"]] = c
        for agent, share in attr.items():
            normalized = share / total_share
            agent_qty = qty * normalized
            mv = agent_qty * price
            conv_entry = conv_lookup.get(agent)
            conv_w = float(conv_entry["weight"]) if conv_entry else None
            direction = None
            if conv_w is not None:
                direction = "long" if conv_w >= 0 else "short"
            else:
                direction = "long" if qty > 0 else "short"
            rows.append({
                "agent_name": agent,
                "symbol": sym,
                "holding_qty": agent_qty,
                "attribution_share": normalized,
                "conviction": conv_w,
                "direction": direction,
                "price_per_share": price,
                "market_value": mv,
            })
            agents_seen.add(agent)

    # 3. CASH rows — split desk cash by normalized conviction weight.
    cash_total_usd = nav * float(cash_weight or 0.0)
    if cash_contributors and cash_total_usd != 0:
        norm: list[tuple[str, float]] = []
        for c in cash_contributors:
            if isinstance(c, dict):
                a, w = c.get("agent"), float(c.get("weight", 0))
            else:
                a, w = c[0], float(c[1])
            if a and w:
                norm.append((a, abs(w)))
        total_w = sum(w for _, w in norm)
        if total_w > 0:
            for a, w in norm:
                share = w / total_w
                cash_for_agent = cash_total_usd * share
                rows.append({
                    "agent_name": a,
                    "symbol": "CASH",
                    "holding_qty": cash_for_agent,
                    "attribution_share": share,
                    "conviction": None,
                    "direction": None,
                    "price_per_share": 1.0,
                    "market_value": cash_for_agent,
                })
                agents_seen.add(a)
    elif cash_total_usd != 0:
        # No conviction-driven cash split — bucket as orphan cash so the
        # row total still reconciles to NAV.
        rows.append({
            "agent_name": "__orphan__",
            "symbol": "CASH",
            "holding_qty": cash_total_usd,
            "attribution_share": 1.0,
            "conviction": None,
            "direction": None,
            "price_per_share": 1.0,
            "market_value": cash_total_usd,
        })
        agents_seen.add("__orphan__")

    # 4. Compute agent_equity (denormalized — sum of market_value per agent).
    equity_by_agent: dict[str, float] = {}
    for r in rows:
        equity_by_agent[r["agent_name"]] = (
            equity_by_agent.get(r["agent_name"], 0.0) + float(r["market_value"])
        )
    for r in rows:
        r["agent_equity"] = equity_by_agent.get(r["agent_name"], 0.0)

    # 5. Insert.
    inserted = await store.record_holdings_snapshot(
        decision_id=decision_id, desk_nav=nav, rows=rows,
    )
    symbols = {r["symbol"] for r in rows}
    return {
        "rows_inserted": inserted,
        "agents": len(agents_seen),
        "symbols": len(symbols),
    }


def _load_sector_map_safe() -> dict:
    """Load agents/sector_map.yaml if present; empty dict otherwise."""
    try:
        from pathlib import Path
        import yaml
        p = Path("agents/sector_map.yaml")
        if not p.exists():
            return {}
        with open(p, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}
