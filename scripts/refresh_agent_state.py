"""Hourly per-agent state snapshot — pure SQL + Polygon, no LLM, no IBKR.

Reads `agent_ledger` (the per-agent double-entry book), aggregates each
agent's open positions with weighted-average cost, fetches current marks
from massive_client (Polygon-compatible), computes cumulative realized +
unrealized P&L, and UPSERTs one row per (agent_name, hour_bucket) into
`agent_state`.

Triggered every hour by cron, every day, regardless of trading hours.
Also triggered by mike's `rebalance_desk` after each live run so post-
rebalance reads see fresh state without waiting for the next hourly tick.

CLI:
    .venv/bin/python -m scripts.refresh_agent_state           # one-shot
    .venv/bin/python -m scripts.refresh_agent_state --json    # also dump summary

Cumulative semantics: realized_pnl is the lifetime sum of every
RETURN/DIVIDEND realized_pnl; unrealized_pnl marks the open book to current
prices. Day-over-day P&L = total_pnl(t1) − total_pnl(t0); the headline
number doesn't move when fills settle (RETURN moves money from unrealized
to realized but their sum is invariant).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Cron strips PATH and env, so source `.env` ourselves before any massive
# / db calls. `override=False` so an explicit env var (testing) wins.
try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env", override=False)
except ImportError:
    pass


log = logging.getLogger("refresh_agent_state")


# ── Aggregate open positions per (agent, symbol) from agent_ledger ──────────

async def _open_positions_per_agent() -> dict[str, dict[str, dict]]:
    """Return {agent_name: {symbol: {qty, avg_cost, realized_to_date}}}.

    Walks LEND/RETURN events per (agent, symbol) maintaining weighted-average
    cost. Pro-rata closes preserve avg_cost (RETURN deducts at the running
    avg, not the sale price), so this matches `record_return_for_fill`'s
    realized_pnl computation by construction.
    """
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT agent_name, UPPER(symbol) AS symbol, event,
                      booked_at, id,
                      qty::float8 AS qty,
                      price_per_share::float8 AS price,
                      realized_pnl::float8 AS realized_pnl
               FROM agent_ledger
               ORDER BY agent_name, UPPER(symbol), booked_at, id"""
        )

    out: dict[str, dict[str, dict]] = {}
    state: dict[tuple, dict] = {}  # (agent, symbol) → {qty, total_cost, realized}
    for r in rows:
        key = (r["agent_name"], r["symbol"])
        st = state.setdefault(key, {"qty": 0.0, "cost": 0.0, "realized": 0.0})
        ev = r["event"]
        q = float(r["qty"] or 0)
        p = float(r["price"] or 0)
        if ev == "LEND":
            st["qty"] += q
            st["cost"] += q * p
        elif ev == "RETURN":
            avg = (st["cost"] / st["qty"]) if st["qty"] > 1e-9 else 0.0
            st["qty"] -= q
            st["cost"] -= q * avg
            if r["realized_pnl"] is not None:
                st["realized"] += float(r["realized_pnl"])
            if st["qty"] < 1e-9:
                st["qty"] = 0.0
                st["cost"] = 0.0
        elif ev == "DIVIDEND":
            if r["realized_pnl"] is not None:
                st["realized"] += float(r["realized_pnl"])

    for (agent, sym), st in state.items():
        agent_dict = out.setdefault(agent, {})
        if st["qty"] > 1e-9:
            avg_cost = st["cost"] / st["qty"]
            agent_dict[sym] = {
                "qty": st["qty"],
                "avg_cost": avg_cost,
                "realized_to_date": st["realized"],
            }
        else:
            # Closed position — realized still counts toward the agent total
            # (we'll fold it in via the per-agent realized rollup query
            # below; here we just skip the empty position row).
            pass
    return out


async def _agent_realized_totals() -> dict[str, float]:
    """Lifetime realized P&L per agent — sum of every RETURN/DIVIDEND
    realized_pnl. Includes both currently-open and fully-closed symbols."""
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT agent_name, COALESCE(SUM(realized_pnl), 0)::float8 AS r
               FROM agent_ledger
               WHERE event IN ('RETURN','DIVIDEND')
               GROUP BY agent_name"""
        )
    return {r["agent_name"]: float(r["r"]) for r in rows}


# ── Polygon batch quote fan-out ─────────────────────────────────────────────

async def _fetch_marks(symbols: list[str]) -> dict[str, float]:
    """Return {symbol: last_or_close_price}. Symbols whose quote fails or
    returns no price are dropped — caller marks them as missing."""
    from data.massive_client import get_quote

    async def _one(sym: str) -> tuple[str, Optional[float]]:
        try:
            q = await get_quote(sym)
        except Exception as exc:
            log.warning("quote failed for %s: %s", sym, exc)
            return sym, None
        last = q.get("last") or q.get("close")
        try:
            return sym, float(last) if last is not None else None
        except (TypeError, ValueError):
            return sym, None

    pairs = await asyncio.gather(*[_one(s) for s in symbols])
    return {s: p for s, p in pairs if p is not None and p > 0}


# ── Build snapshot rows ─────────────────────────────────────────────────────

async def _build_state_rows() -> tuple[list[dict], dict]:
    """Returns (rows, summary). Rows are ready for store.record_agent_state."""
    by_agent = await _open_positions_per_agent()
    realized_totals = await _agent_realized_totals()

    # Union of all agents seen (open positions OR any settled realized).
    all_agents = set(by_agent.keys()) | set(realized_totals.keys())
    if not all_agents:
        return [], {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "n_agents": 0, "n_symbols": 0, "missing_marks": [],
        }

    # Collect all distinct symbols across agents → one Polygon fan-out.
    distinct_symbols = sorted({
        sym for syms in by_agent.values() for sym in syms.keys()
    })
    marks = await _fetch_marks(distinct_symbols) if distinct_symbols else {}
    missing_marks = [s for s in distinct_symbols if s not in marks]

    rows: list[dict] = []
    for agent in sorted(all_agents):
        positions = by_agent.get(agent, {})
        realized_pnl = realized_totals.get(agent, 0.0)
        unrealized_pnl = 0.0
        open_cost = 0.0
        open_market_value = 0.0
        positions_json: list[dict] = []

        for sym, p in sorted(positions.items()):
            qty = p["qty"]
            avg = p["avg_cost"]
            mark = marks.get(sym, 0.0)
            mv = qty * mark
            unrl = qty * (mark - avg) if mark > 0 else 0.0
            open_cost += qty * avg
            open_market_value += mv
            unrealized_pnl += unrl
            positions_json.append({
                "sym": sym,
                "qty": round(qty, 6),
                "avg_cost": round(avg, 4),
                "mark": round(mark, 4),
                "market_value": round(mv, 2),
                "unrealized": round(unrl, 2),
            })

        rows.append({
            "agent_name": agent,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "total_pnl": realized_pnl + unrealized_pnl,
            "open_cost": open_cost,
            "open_market_value": open_market_value,
            "n_positions": len(positions_json),
            "positions_json": positions_json,
        })

    summary = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "n_agents": len(rows),
        "n_symbols": len(distinct_symbols),
        "missing_marks": missing_marks,
    }
    return rows, summary


async def refresh() -> dict:
    """Build + UPSERT one agent_state row per agent for the current hour
    bucket. Returns a summary dict."""
    from db import store as _store
    rows, summary = await _build_state_rows()
    if not rows:
        log.info("refresh_agent_state: no agents in ledger; nothing to write")
        return {**summary, "rows_written": 0}
    n = await _store.record_agent_state(rows)
    return {**summary, "rows_written": n,
            "agents": [r["agent_name"] for r in rows]}


# ── CLI ────────────────────────────────────────────────────────────────────

async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit summary as JSON")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        summary = await refresh()
    except Exception as exc:
        log.exception("refresh_agent_state failed: %s", exc)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(
            f"refresh_agent_state: wrote {summary.get('rows_written', 0)} rows "
            f"({summary.get('n_agents', 0)} agents × {summary.get('n_symbols', 0)} distinct symbols)"
        )
        if summary.get("missing_marks"):
            print(f"  missing marks: {summary['missing_marks']}")
        if summary.get("rows_written", 0) == 0:
            print("  (ledger empty — agents will populate after mike's first post-deploy run)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
