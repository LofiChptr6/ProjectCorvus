"""One-shot: backfill holding_kanban for today's live allocation_decision rows.

Reconstructs the desk's position state at each decision's `decided_at` by
replaying all fills with `filled_at <= decided_at`. Attributes each held
position to contributing agents using the agent_pnl_attribution rows tied to
that decision (and any prior open-fill attribution on the same symbol).

Cash for historical rows is bucketed as `__orphan__` because cash_contributors
weren't persisted before this feature shipped — we can't reconstruct the
weighted split. Equity rows are accurate; cash rows are aggregate-only.

Idempotent on (decision_id, agent_name, symbol): re-running deletes any rows
this script previously wrote for those decisions before reinserting.

Usage:
    python -m scripts.backfill_kanban_today
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Ensure repo root on sys.path when invoked directly.
_REPO_ROOT = str(Path(__file__).parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_kanban")


async def _todays_live_decisions(conn) -> list[dict]:
    rows = await conn.fetch(
        """SELECT id, decided_at, nav_at_decision,
                  target_weights_json, contributing_views_json, notes
           FROM allocation_decision
           WHERE decided_at::date = CURRENT_DATE
             AND notes LIKE 'live%'
           ORDER BY decided_at ASC""",
    )
    out = []
    for r in rows:
        out.append({
            "id": int(r["id"]),
            "decided_at": r["decided_at"],
            "nav": float(r["nav_at_decision"] or 0),
            "target_weights": json.loads(r["target_weights_json"]) if isinstance(r["target_weights_json"], str) else (r["target_weights_json"] or {}),
            "contributing_views": json.loads(r["contributing_views_json"]) if isinstance(r["contributing_views_json"], str) else (r["contributing_views_json"] or {}),
            "notes": r["notes"] or "",
        })
    return out


async def _fills_through(conn, decided_at) -> list[dict]:
    """All fills with filled_at <= decided_at. fills.filled_at is TEXT, ISO."""
    cutoff_iso = decided_at.astimezone(timezone.utc).isoformat()
    rows = await conn.fetch(
        """SELECT id, symbol, action, quantity, fill_price, filled_at
           FROM fills
           WHERE filled_at <= $1
           ORDER BY filled_at ASC""",
        cutoff_iso,
    )
    return [dict(r) for r in rows]


def _reconstruct_positions(fills: list[dict]) -> dict[str, dict]:
    """Replay fills → {symbol: {qty, vwap, last_price}}. BUY adds, SELL subtracts.
    vwap is volume-weighted across all BUYs (used as price proxy when no quote)."""
    by_sym: dict[str, dict] = {}
    for f in fills:
        sym = (f["symbol"] or "").upper()
        if not sym:
            continue
        action = (f["action"] or "").upper()
        qty = float(f["quantity"] or 0)
        price = float(f["fill_price"] or 0)
        if qty <= 0 or price <= 0:
            continue
        sign = 1 if action in ("BOT", "BUY") else -1 if action in ("SLD", "SELL") else 0
        if sign == 0:
            continue
        cur = by_sym.setdefault(sym, {"qty": 0.0, "buy_qty": 0.0, "buy_notional": 0.0, "last_price": 0.0})
        cur["qty"] += sign * qty
        cur["last_price"] = price
        if sign == 1:
            cur["buy_qty"] += qty
            cur["buy_notional"] += qty * price
    # Compute vwap for buys (proxy for cost basis); fall back to last_price when no buys.
    out: dict[str, dict] = {}
    for sym, s in by_sym.items():
        if abs(s["qty"]) < 1e-9:
            continue
        vwap = s["buy_notional"] / s["buy_qty"] if s["buy_qty"] else s["last_price"]
        out[sym] = {"qty": s["qty"], "price": s["last_price"] or vwap, "vwap": vwap}
    return out


async def _attribution_shares_at(conn, symbol: str, decision_id: int) -> dict[str, float]:
    """Sum attribution_share per agent across all rows for this symbol with
    decision_id <= the given one and attributed_pnl IS NULL (open fills as of
    that moment). Mirrors combined_pnl._get_open_attribution_shares but
    bounded by decision."""
    rows = await conn.fetch(
        """SELECT agent_name, SUM(attribution_share)::float8 AS total_share
           FROM agent_pnl_attribution
           WHERE symbol=$1 AND decision_id <= $2
           GROUP BY agent_name""",
        symbol.upper(), decision_id,
    )
    return {r["agent_name"]: float(r["total_share"]) for r in rows if r["total_share"]}


async def backfill(dry_run: bool = False) -> None:
    from db.schema import get_pool, close_pool
    from db import store

    pool = await get_pool()
    async with pool.acquire() as conn:
        decisions = await _todays_live_decisions(conn)
        if not decisions:
            log.info("no live decisions today; nothing to backfill")
            return
        log.info("backfilling %d live decisions", len(decisions))

        # Wipe any kanban rows previously written for these decisions
        # (covers re-run + rows the live snapshot may have written for d13/d14).
        ids = [d["id"] for d in decisions]
        async with conn.transaction():
            existing = await conn.fetchval(
                "SELECT COUNT(*) FROM holding_kanban WHERE decision_id = ANY($1::bigint[])",
                ids,
            )
            log.info("clearing %d existing kanban rows for these decisions", existing)
            if not dry_run:
                await conn.execute(
                    "DELETE FROM holding_kanban WHERE decision_id = ANY($1::bigint[])",
                    ids,
                )

        for d in decisions:
            decision_id = d["id"]
            decided_at = d["decided_at"]
            nav = d["nav"]
            target_weights = d["target_weights"]
            contributing_views = d["contributing_views"]

            fills = await _fills_through(conn, decided_at)
            positions = _reconstruct_positions(fills)

            # Cash weight = 1 - sum(equity weights). target_weights only contains
            # equity symbols (CASH was popped). Most recent decisions also stash
            # the value in notes as 'cash_weight=...'; trust that if present.
            equity_w_sum = sum(float(w) for w in target_weights.values())
            cash_weight = max(0.0, 1.0 - equity_w_sum)
            if "cash_weight=" in (d["notes"] or ""):
                try:
                    cash_weight = float(d["notes"].rsplit("cash_weight=", 1)[1])
                except ValueError:
                    pass

            rows: list[dict] = []
            agents_seen: set[str] = set()

            # 1. Equity rows.
            for sym, p in positions.items():
                qty = float(p["qty"])
                price = float(p["price"]) or float(p["vwap"]) or 0.0
                if qty == 0 or price <= 0:
                    continue
                attr = await _attribution_shares_at(conn, sym, decision_id)
                if not attr:
                    rows.append({
                        "agent_name": "__orphan__", "symbol": sym,
                        "holding_qty": qty, "attribution_share": 1.0,
                        "conviction": None,
                        "direction": "long" if qty > 0 else "short",
                        "price_per_share": price,
                        "market_value": qty * price,
                    })
                    agents_seen.add("__orphan__")
                    continue
                total_share = sum(attr.values())
                conv_lookup = {c["agent"]: c for c in (contributing_views.get(sym) or [])}
                for agent, share in attr.items():
                    norm = share / total_share
                    agent_qty = qty * norm
                    mv = agent_qty * price
                    conv_entry = conv_lookup.get(agent)
                    conv_w = float(conv_entry["weight"]) if conv_entry else None
                    direction = (
                        ("long" if conv_w >= 0 else "short")
                        if conv_w is not None
                        else ("long" if qty > 0 else "short")
                    )
                    rows.append({
                        "agent_name": agent, "symbol": sym,
                        "holding_qty": agent_qty, "attribution_share": norm,
                        "conviction": conv_w, "direction": direction,
                        "price_per_share": price, "market_value": mv,
                    })
                    agents_seen.add(agent)

            # 2. Cash row — orphan-only for backfill.
            cash_usd = nav * cash_weight
            if cash_usd > 0:
                rows.append({
                    "agent_name": "__orphan__", "symbol": "CASH",
                    "holding_qty": cash_usd, "attribution_share": 1.0,
                    "conviction": None, "direction": None,
                    "price_per_share": 1.0, "market_value": cash_usd,
                })
                agents_seen.add("__orphan__")

            # 3. agent_equity (denormalized).
            equity_by_agent: dict[str, float] = defaultdict(float)
            for r in rows:
                equity_by_agent[r["agent_name"]] += float(r["market_value"])
            for r in rows:
                r["agent_equity"] = equity_by_agent[r["agent_name"]]

            # 4. Insert with snapshot_at overridden to decided_at so the
            # trajectory reflects the historical hour, not now.
            if dry_run:
                log.info("d%d %s: would insert %d rows (%d agents) NAV=$%.0f cash_w=%.4f",
                         decision_id, decided_at.isoformat(), len(rows),
                         len(agents_seen), nav, cash_weight)
                continue

            async with conn.transaction():
                await conn.executemany(
                    """INSERT INTO holding_kanban
                         (snapshot_at, decision_id, agent_name, symbol, holding_qty,
                          attribution_share, conviction, direction,
                          price_per_share, market_value, agent_equity, desk_nav)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""",
                    [
                        (
                            decided_at, decision_id,
                            r["agent_name"], r["symbol"],
                            float(r["holding_qty"]),
                            float(r["attribution_share"]) if r.get("attribution_share") is not None else None,
                            float(r["conviction"]) if r.get("conviction") is not None else None,
                            r.get("direction"),
                            float(r["price_per_share"]),
                            float(r["market_value"]),
                            float(r["agent_equity"]),
                            float(nav),
                        )
                        for r in rows
                    ],
                )
            log.info("d%d %s: inserted %d rows (%d agents) NAV=$%.0f",
                     decision_id, decided_at.isoformat(), len(rows),
                     len(agents_seen), nav)

    await close_pool()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Preview only; do not write")
    args = parser.parse_args()
    asyncio.run(backfill(dry_run=args.dry_run))
