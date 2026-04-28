"""FIFO close-attribution.

When a fill closes a prior position, realized P&L belongs on the OPENING
decision's `agent_pnl_attribution` rows — not the closing decision (whose
SELL/BUY-to-cover orders carry no contributing_view, so no rows exist for
them). This module owns the FIFO match.

Two entry points:
  - `compute_close_events(fills, decision_for_order)`: pure FIFO; returns
    (closing_fill, opening_decision, symbol, realized_pnl) tuples.
  - `reconcile_symbol(conn, symbol, since=None)`: idempotent DB write —
    walks fills for one symbol, computes events, calls add_attributed_pnl
    only on rows still NULL. Used both by `_on_fill` (after each fill) and
    by the offline reconciler script.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque

log = logging.getLogger(__name__)


def compute_close_events(
    fills: list[dict],
    decision_for_order: dict[int, int],
) -> list[dict]:
    """FIFO-match per symbol. Each lot remembers the opening fill's
    decision_id. Returns one event per (closing_fill, opening_decision)
    pair with the realized P&L attributable to that opening lot.

    Convention: BOT/BUY pushes a long lot or covers a short lot; SLD/SELL
    pops a long lot or pushes a short lot. Lots without a known opening
    decision (manual orders, pre-allocator fills) are tracked but their
    closes are skipped — there's nowhere to write the P&L."""
    out: list[dict] = []
    by_sym: dict[str, deque] = defaultdict(deque)  # (qty, price, opening_decision_id)

    for f in fills:
        sym = (f["symbol"] or "").upper()
        action = (f["action"] or "").upper()
        qty = float(f["quantity"] or 0)
        price = float(f["fill_price"] or 0)
        order_id = int(f["order_id"]) if f["order_id"] is not None else None
        if qty <= 0 or price <= 0 or sym == "":
            continue
        sign = 1 if action in ("BOT", "BUY") else -1 if action in ("SLD", "SELL") else 0
        if sign == 0:
            continue

        lots = by_sym[sym]
        remaining = qty * sign
        opening_decision = decision_for_order.get(order_id) if order_id else None

        while lots and remaining != 0 and (lots[0][0] * remaining) < 0:
            lot_qty, lot_price, lot_decision = lots[0]
            close_qty = min(abs(lot_qty), abs(remaining))
            if lot_qty > 0:
                pnl = (price - lot_price) * close_qty
            else:
                pnl = (lot_price - price) * close_qty
            if abs(pnl) >= 0.01 and lot_decision is not None:
                out.append({
                    "fill_id": int(f["id"]),
                    "opening_decision_id": lot_decision,
                    "symbol": sym,
                    "realized_pnl": pnl,
                })
            new_lot_qty = lot_qty + (close_qty if lot_qty < 0 else -close_qty)
            if abs(new_lot_qty) < 1e-9:
                lots.popleft()
            else:
                lots[0] = (new_lot_qty, lot_price, lot_decision)
            remaining = remaining + (close_qty if remaining < 0 else -close_qty)

        if remaining != 0:
            lots.append((remaining, price, opening_decision))

    return out


async def reconcile_symbol(symbol: str, since: str | None = None) -> dict:
    """Walk every fill for `symbol` (optionally since an ISO timestamp),
    FIFO-match, and back-fill any missing attribution rows. Idempotent —
    rows already carrying a non-NULL `attributed_pnl` are skipped.

    Intended to be called both from `_on_fill` (per-symbol after each fill)
    and from the offline reconciler (per-symbol or whole-history)."""
    from db.schema import get_pool
    from db import store

    pool = await get_pool()
    summary = {"symbol": symbol, "events": 0, "rows_updated": 0, "skipped_already_set": 0, "skipped_no_rows": 0}

    async with pool.acquire() as conn:
        if since:
            fills = await conn.fetch(
                """SELECT id, order_id, symbol, action, quantity, fill_price, filled_at
                   FROM fills
                   WHERE symbol = $1 AND filled_at >= $2
                   ORDER BY filled_at ASC, id ASC""",
                symbol.upper(), since,
            )
        else:
            fills = await conn.fetch(
                """SELECT id, order_id, symbol, action, quantity, fill_price, filled_at
                   FROM fills
                   WHERE symbol = $1
                   ORDER BY filled_at ASC, id ASC""",
                symbol.upper(),
            )
        fills_d = [dict(r) for r in fills]
        if not fills_d:
            return summary

        order_ids = sorted({int(f["order_id"]) for f in fills_d if f["order_id"] is not None})
        decision_for_order: dict[int, int] = {}
        for oid in order_ids:
            d = await store.get_decision_id_for_order(oid)
            if d is not None:
                decision_for_order[oid] = d

        events = compute_close_events(fills_d, decision_for_order)
        summary["events"] = len(events)

        for ev in events:
            decision_id = ev["opening_decision_id"]
            row = await conn.fetchrow(
                """SELECT COUNT(*) AS n_rows, COUNT(attributed_pnl) AS n_set
                   FROM agent_pnl_attribution
                   WHERE decision_id = $1 AND symbol = $2""",
                decision_id, ev["symbol"],
            )
            if not row or row["n_rows"] == 0:
                summary["skipped_no_rows"] += 1
                continue
            if row["n_set"] == row["n_rows"]:
                summary["skipped_already_set"] += 1
                continue
            updated = await store.add_attributed_pnl(
                decision_id, ev["symbol"], ev["realized_pnl"],
            )
            summary["rows_updated"] += updated

    return summary
