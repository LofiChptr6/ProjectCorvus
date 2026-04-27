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
    # P&L by agent
    pnl_rows = await store.get_pnl_summary(period="today")
    agents = list_agents(enabled_only=False)

    total_realized = sum(r.get("realized_pnl", 0) or 0 for r in pnl_rows)
    total_fills = len(all_fills)

    lines = [
        f"╔══════════════════════════════════════════════╗",
        f"║  DAILY TRADING REPORT  {trade_date}          ║",
        f"╚══════════════════════════════════════════════╝",
        "",
        "── PORTFOLIO SUMMARY ──────────────────────────",
        f"  Total realized P&L:  ${total_realized:>+10,.2f}",
        f"  Total fills:          {total_fills:>4}",
        "",
        "── PER-AGENT P&L ──────────────────────────────",
    ]

    pnl_map = {r["agent_name"]: r for r in pnl_rows}
    for agent in agents:
        name = agent["name"]
        r = pnl_map.get(name)
        if r:
            lines.append(
                f"  {name:<14} realized=${r['realized_pnl']:>+9,.2f}  "
                f"unrealized=${r['unrealized_pnl']:>+9,.2f}  fills={r['num_fills']}"
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
