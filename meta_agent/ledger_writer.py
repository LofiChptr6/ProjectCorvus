"""Hook called by `ibkr/daemon.py:_on_fill` after every fill the broker
reports. Translates the fill into per-agent `agent_ledger` events:

  - BUY  → LEND rows (qty proportional to each contributing agent's
           normalized conviction at decision time)
  - SELL → RETURN rows (pro-rata across agents currently holding the
           symbol; per DESK_POLICY §0)

Fills with no allocation_decision backing (manual orders, kill-switch
closes, anything outside mike's allocator pipeline) stay on mike's book
implicitly — no ledger rows are written, so the position doesn't get
attributed to any agent.

Intentionally separate from `meta_agent/allocator.py`: that module owns
conviction → target weight math; this module owns fill → ledger event math.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

log = logging.getLogger(__name__)


async def book_fill_to_ledger(
    *,
    fill_id: int,
    order_id: Optional[int],
    symbol: str,
    action: str,
    quantity: float,
    fill_price: float,
    booked_at=None,
) -> dict:
    """Translate one broker fill into per-agent ledger rows.

    Returns a small summary dict — primarily for logging — like:
        {"event": "LEND" | "RETURN" | "ORPHAN",
         "rows_written": N,
         "agents": [...],
         "decision_id": int | None}

    Idempotency: ledger rows have no UNIQUE constraint on fill_id (a single
    BUY produces N LEND rows, one per contributing agent). Re-running this
    on the same fill would double-write. Caller is responsible for not
    invoking twice — `_on_fill` is invoked once per fill by the IBKR daemon
    via the unique exec_id INSERT.
    """
    from db import store

    sym_u = (symbol or "").upper()
    side = (action or "").upper()
    if side in ("BOT",):
        side = "BUY"
    elif side in ("SLD",):
        side = "SELL"

    # 1. Find the allocation_decision behind this fill (None for manual).
    decision_id = None
    if order_id is not None:
        try:
            decision_id = await store.get_decision_id_for_order(order_id)
        except Exception as exc:
            log.warning("get_decision_id_for_order(%s) failed: %s", order_id, exc)

    if decision_id is None:
        log.info(
            "fill_id=%s %s %s %s @ %s has no allocation_decision; orphan to mike",
            fill_id, side, sym_u, quantity, fill_price,
        )
        return {"event": "ORPHAN", "rows_written": 0,
                "agents": [], "decision_id": None}

    # 2. SELL → pro-rata close across current holders (deterministic on the
    # current ledger state). No need for contributors lookup.
    if side == "SELL":
        result = await store.record_return_for_fill(
            fill_id=fill_id, decision_id=decision_id,
            symbol=sym_u, fill_qty=float(quantity),
            fill_price=float(fill_price),
            booked_at=booked_at,
        )
        return {
            "event": "RETURN",
            "rows_written": result.get("rows_written", 0),
            "agents": [a["agent"] for a in result.get("agents", [])],
            "orphan_qty": result.get("orphan_qty", 0.0),
            "decision_id": decision_id,
        }

    # 3. BUY → LEND rows weighted by conviction shares from the decision.
    contribs = await _contributors_for_held_symbol(decision_id, sym_u)
    if not contribs:
        log.warning(
            "fill_id=%s BUY %s @ %s has decision_id=%s but no contributors "
            "for the held symbol; orphan to mike",
            fill_id, sym_u, fill_price, decision_id,
        )
        return {"event": "ORPHAN", "rows_written": 0,
                "agents": [], "decision_id": decision_id}

    # split_attribution normalizes |conviction| across agents who voted on
    # this side (positive weights for BUY, negative for SELL).
    from meta_agent.allocator import split_attribution
    shares = split_attribution(contribs, "BUY")
    if not shares:
        log.info(
            "fill_id=%s BUY %s: no agents on the BUY side after split; orphan",
            fill_id, sym_u,
        )
        return {"event": "ORPHAN", "rows_written": 0,
                "agents": [], "decision_id": decision_id}

    n = await store.record_lend_for_fill(
        fill_id=fill_id, decision_id=decision_id,
        symbol=sym_u, fill_qty=float(quantity),
        fill_price=float(fill_price),
        agent_shares=shares,
        booked_at=booked_at,
    )
    return {
        "event": "LEND",
        "rows_written": n,
        "agents": [a for (a, _) in shares],
        "decision_id": decision_id,
    }


async def _contributors_for_held_symbol(
    decision_id: int,
    held_symbol: str,
) -> list:
    """Look up the contributing-views list for `held_symbol` from the
    `allocation_decision.contributing_views_json`. Handles inverse-ETF
    mapping: if the decision targeted a bearish view on QQQ that resolved
    to SQQQ (the held vehicle), the contributors live under 'QQQ' in the
    JSON — we resolve that here.

    Returns the contributors list as `[(agent_name, signed_weight), ...]`
    in the shape `split_attribution` expects."""
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT contributing_views_json
               FROM allocation_decision WHERE id = $1""",
            decision_id,
        )
    if not row:
        return []
    cv = row["contributing_views_json"]
    if isinstance(cv, str):
        cv = json.loads(cv)
    cv = cv or {}

    # Direct hit on the held symbol.
    direct = cv.get(held_symbol) or cv.get(held_symbol.upper())
    if direct:
        return [(c["agent"], float(c["weight"])) for c in direct]

    # Inverse-ETF lookup: scan for any entry whose bearish resolution is
    # `held_symbol`.
    try:
        from meta_agent.allocator import resolve_bearish_vehicle
        from db import store
        sector_map = await store.load_watchlist_as_sector_map()
        for orig, views in cv.items():
            if not views:
                continue
            try:
                v, mode = resolve_bearish_vehicle(orig, sector_map)
            except Exception:
                continue
            if mode == "inverse_etf" and v.upper() == held_symbol.upper():
                return [(c["agent"], float(c["weight"])) for c in views]
    except Exception as exc:
        log.warning("inverse-ETF lookup failed for %s: %s", held_symbol, exc)

    return []
