"""Assembles the daily trading report."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import db.store as store
from agent.agent_registry import list_agents


async def generate(trade_date: Optional[str] = None, output_dir: str = "data/reports") -> str:
    if trade_date is None:
        trade_date = date.today().isoformat()

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # All fills for the day
    all_fills = await store.get_fills(date=trade_date, limit=500)
    # Combined (realized + unrealized) per agent — reads latest agent_state snapshot.
    from reporting.agent_pnl import get_pnl_combined
    combined = await get_pnl_combined()
    pnl_rows = combined["rows"]
    desk = combined["desk"]
    agents = list_agents(enabled_only=False)

    total_pnl = float(desk["combined_total"])
    realized_total = float(desk["realized_total"])
    unrealized_total = float(desk["unrealized_total"])
    # ledger model has no commission_gap or orphan_unreal; left here as 0 for
    # backwards-compat in the report layout.
    commission_gap = 0.0
    orphan_unreal = 0.0
    total_fills = len(all_fills)

    starting_nav = await _starting_nav(trade_date)
    spy_return_pct = await _spy_day_return_pct(trade_date)

    lines = [
        f"╔══════════════════════════════════════════════╗",
        f"║  DAILY TRADING REPORT  {trade_date}          ║",
        f"╚══════════════════════════════════════════════╝",
        "",
        "── PORTFOLIO SUMMARY ──────────────────────────",
        f"  Total P&L (real + unreal): ${total_pnl:>+10,.2f}",
        f"    realized:                ${realized_total:>+10,.2f}",
        f"    unrealized:              ${unrealized_total:>+10,.2f}",
        f"    commission/fees gap:     ${commission_gap:>+10,.2f}",
        f"    orphan unrealized:       ${orphan_unreal:>+10,.2f}",
        f"  Total fills:               {total_fills:>4}",
    ]

    if starting_nav and starting_nav > 0:
        desk_return_pct = 100.0 * total_pnl / starting_nav
        if spy_return_pct is not None:
            excess_bp = (desk_return_pct - spy_return_pct) * 100.0
            lines.append(
                f"  Desk: {desk_return_pct:+.2f}%  |  SPY: {spy_return_pct:+.2f}%  "
                f"|  excess: {excess_bp:+.0f}bp"
            )
        else:
            lines.append(f"  Desk return:             {desk_return_pct:+.2f}%")
    elif spy_return_pct is not None:
        lines.append(f"  SPY: {spy_return_pct:+.2f}% (desk return n/a — starting NAV unknown)")

    lines.extend(["", "── PER-AGENT P&L ──────────────────────────────"])

    pnl_map = {r["agent_name"]: r for r in pnl_rows}
    for agent in agents:
        name = agent["name"]
        r = pnl_map.get(name)
        if r:
            lines.append(
                f"  {name:<14} P&L=${r['total_pnl']:>+9,.2f}  "
                f"(real ${r.get('realized_pnl', 0):+,.2f}, unreal ${r.get('unrealized_pnl', 0):+,.2f})  "
                f"fills={r['num_fills']}"
            )
        else:
            lines.append(f"  {name:<14} no activity")

    lines.extend(["", "── FILL BLOTTER ────────────────────────────────"])
    if all_fills:
        lines.append(f"  {'Time':<20} {'Agent':<14} {'Sym':<6} {'Side':<4} {'Qty':>6} {'Price':>8} {'Comm':>6}")
        lines.append("  " + "-" * 70)
        for f in all_fills:
            t = f.get("filled_at", "")[:19]
            comm = f"${f['commission']:.2f}" if f.get("commission") else "-"
            lines.append(
                f"  {t:<20} {(f.get('agent_name') or '-'):<14} "
                f"{f['symbol']:<6} {f['action']:<4} {f['quantity']:>6.0f} "
                f"${f['fill_price']:>7.2f} {comm:>6}"
            )
    else:
        lines.append("  No fills today.")

    report_text = "\n".join(lines)

    # Write text report
    txt_path = Path(output_dir) / f"{trade_date}.txt"
    txt_path.write_text(report_text)

    # Write CSV
    csv_path = Path(output_dir) / f"{trade_date}_fills.csv"
    if all_fills:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(all_fills[0].keys()))
        writer.writeheader()
        writer.writerows(all_fills)
        csv_path.write_text(buf.getvalue())

    return report_text


async def _starting_nav(trade_date: str) -> Optional[float]:
    """First nav_at_decision recorded today — Mike's allocator stamps NAV on
    every rebalance, so the earliest one is a good proxy for session-open NAV."""
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT nav_at_decision::float8 AS nav
               FROM allocation_decision
               WHERE decided_at::date = $1::date
               ORDER BY decided_at ASC LIMIT 1""",
            date.fromisoformat(trade_date),
        )
        return float(row["nav"]) if row and row["nav"] is not None else None


async def _spy_day_return_pct(trade_date: str) -> Optional[float]:
    """SPY (close - prior_close) / prior_close × 100. Returns None on any
    failure — daily report still renders the rest of the page."""
    try:
        from data.massive_client import get_bars
        bars = (await get_bars("SPY", "1 day", "5 D")).get("bars") or []
        on_or_before = [b for b in bars if (b.get("t") or "")[:10] <= trade_date]
        if len(on_or_before) < 2:
            return None
        prev_close = on_or_before[-2].get("c")
        cur_close = on_or_before[-1].get("c")
        if not prev_close or not cur_close:
            return None
        return 100.0 * (cur_close - prev_close) / prev_close
    except Exception:
        return None
