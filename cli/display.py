"""Rich terminal display helpers."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich import box

console = Console()


def positions_table(positions: list[dict]) -> Table:
    t = Table(title="Positions", box=box.ROUNDED, show_header=True, header_style="bold cyan")
    t.add_column("Symbol", style="white bold")
    t.add_column("Qty", justify="right")
    t.add_column("Avg Cost", justify="right")
    t.add_column("Market Price", justify="right")
    t.add_column("Unreal P&L", justify="right")
    for p in positions:
        unreal = p.get("unrealized_pnl", 0) or 0
        color = "green" if unreal >= 0 else "red"
        t.add_row(
            p["symbol"],
            f"{p['quantity']:,.0f}",
            f"${p['avg_cost']:,.2f}",
            f"${p.get('market_price', 0):,.2f}" if p.get("market_price") else "-",
            f"[{color}]${unreal:+,.2f}[/{color}]",
        )
    return t


def orders_table(orders: list[dict]) -> Table:
    t = Table(title="Open Orders", box=box.ROUNDED, header_style="bold yellow")
    t.add_column("ID", justify="right")
    t.add_column("Symbol")
    t.add_column("Side")
    t.add_column("Qty", justify="right")
    t.add_column("Filled", justify="right")
    t.add_column("Price", justify="right")
    t.add_column("Status")
    for o in orders:
        price = f"${o['limit_price']:,.2f}" if o.get("limit_price") else "MKT"
        t.add_row(
            str(o["order_id"]),
            o["symbol"],
            o["action"],
            f"{o['quantity']:,.0f}",
            f"{o['filled']:,.0f}",
            price,
            o["status"],
        )
    return t


def pnl_table(rows: list[dict], title: str = "P&L Summary") -> Table:
    t = Table(title=title, box=box.ROUNDED, header_style="bold magenta")
    t.add_column("Agent")
    t.add_column("Date")
    t.add_column("Realized", justify="right")
    t.add_column("Unrealized", justify="right")
    t.add_column("Total", justify="right")
    t.add_column("Fills", justify="right")
    for r in rows:
        total = r.get("total_pnl", 0) or 0
        color = "green" if total >= 0 else "red"
        t.add_row(
            r.get("agent_name", "-"),
            r.get("trade_date", "-"),
            f"${r.get('realized_pnl', 0):+,.2f}",
            f"${r.get('unrealized_pnl', 0):+,.2f}",
            f"[{color}]${total:+,.2f}[/{color}]",
            str(r.get("num_fills", 0)),
        )
    return t


def allocations_table(rows: list[dict]) -> Table:
    t = Table(title="Agent Allocations", box=box.ROUNDED, header_style="bold blue")
    t.add_column("Agent")
    t.add_column("Enabled")
    t.add_column("% NAV", justify="right")
    t.add_column("≈ USD", justify="right")
    t.add_column("Source")
    t.add_column("Updated")
    total_pct = 0.0
    for r in rows:
        if r.get("enabled"):
            total_pct += r.get("allocation_pct", 0)
        t.add_row(
            r["agent_name"],
            "✓" if r.get("enabled") else "✗",
            f"{r.get('allocation_pct', 0):.1%}",
            f"${r.get('allocated_usd', 0):,.0f}",
            r.get("source", "-"),
            (r.get("updated_at") or "-")[:19],
        )
    t.caption = f"Enabled total: {total_pct:.1%}  |  Idle cash: {max(0.0, 1.0 - total_pct):.1%}"
    return t
