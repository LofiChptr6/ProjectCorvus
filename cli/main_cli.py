"""Main CLI entry point: trade <command>"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

import typer
import yaml
from dotenv import load_dotenv
from rich.console import Console

load_dotenv()
app = typer.Typer(help="IBKR Agentic Trading System", no_args_is_help=True)
console = Console()

_cfg: dict = {}


def _load_config(config_path: str = "config.yaml") -> dict:
    global _cfg
    if not _cfg:
        p = Path(config_path)
        if not p.exists():
            console.print(f"[red]Config not found: {config_path}. Copy config.example.yaml → config.yaml[/red]")
            raise SystemExit(1)
        with open(p) as f:
            _cfg = yaml.safe_load(f)
    return _cfg


def _run(coro):
    return asyncio.run(coro)


# Trading routines are run by Claude Code via the MCP server (mcp_server.py),
# not by this CLI. The `run` and `schedule` commands were removed along with the
# Anthropic API client. To invoke a routine: connect Claude Code to this MCP
# server and ask the relevant agent to run its routine.


# ── trade status ──────────────────────────────────────────────────────────────

@app.command()
def status(
    agent: Optional[str] = typer.Option(None, "--agent", help="Filter by agent"),
    config: str = typer.Option("config.yaml", "--config"),
):
    """Show current positions, open orders, and today's P&L."""
    cfg = _load_config(config)

    async def _show():
        from db.schema import init_db
        from ibkr.client import configure, get_ib
        from ibkr.account import get_positions, get_open_orders
        import db.store as store
        from cli.display import console, positions_table, orders_table, pnl_table

        await init_db()
        configure(cfg)
        await get_ib()

        positions = await get_positions()
        orders = await get_open_orders()
        pnl_rows = await store.get_pnl_summary(agent_name=agent, period="today")

        if positions:
            console.print(positions_table(positions))
        else:
            console.print("[dim]No open positions.[/dim]")

        if orders:
            console.print(orders_table(orders))
        else:
            console.print("[dim]No open orders.[/dim]")

        if pnl_rows:
            console.print(pnl_table(pnl_rows, "Today's P&L"))
        else:
            console.print("[dim]No P&L data today.[/dim]")

    _run(_show())


# ── trade allocate ────────────────────────────────────────────────────────────

@app.command()
def allocate(
    agent: Optional[str] = typer.Argument(None),
    pct: Optional[float] = typer.Argument(None, help="Percentage of NAV (0.0–1.0, e.g. 0.25 for 25%)"),
    show: bool = typer.Option(False, "--show"),
    config: str = typer.Option("config.yaml", "--config"),
):
    """Set agent capital allocation as % of NAV, or show all allocations.

    Example: trade allocate rex 0.30   # gives Rex 30% of NAV
    """
    _load_config(config)

    async def _alloc():
        from db.schema import init_db
        from meta_agent.allocation_manager import get_all_allocations, set_allocation
        from cli.display import console, allocations_table
        await init_db()

        if show or (agent is None):
            rows = await get_all_allocations()
            console.print(allocations_table(rows))
            return

        if pct is None:
            console.print("[red]Provide pct: trade allocate <agent> <pct>  (e.g. 0.25 for 25%)[/red]")
            raise SystemExit(1)
        if not 0.0 <= pct <= 1.0:
            console.print(f"[red]pct must be 0.0–1.0, got {pct}[/red]")
            raise SystemExit(1)

        await set_allocation(agent, pct)
        console.print(f"[green]Set {agent} allocation to {pct:.1%} of NAV[/green]")

    _run(_alloc())



# ── trade report ──────────────────────────────────────────────────────────────

@app.command()
def report(
    date: Optional[str] = typer.Option(None, "--date", help="YYYY-MM-DD (default: today)"),
    config: str = typer.Option("config.yaml", "--config"),
):
    """Generate the daily trading report."""
    cfg = _load_config(config)

    async def _report():
        from db.schema import init_db
        from reporting.daily_report import generate
        await init_db()
        output_dir = cfg.get("reporting", {}).get("output_dir", "data/reports")
        text = await generate(trade_date=date, output_dir=output_dir)
        console.print(text)

    _run(_report())


# ── trade blotter ─────────────────────────────────────────────────────────────

@app.command()
def blotter(
    agent: Optional[str] = typer.Option(None, "--agent"),
    date: Optional[str] = typer.Option(None, "--date"),
    today: bool = typer.Option(False, "--today"),
    csv_out: bool = typer.Option(False, "--csv"),
    config: str = typer.Option("config.yaml", "--config"),
):
    """Show fill history."""
    _load_config(config)

    async def _blotter():
        from db.schema import init_db
        import db.store as store
        from datetime import date as dt_date
        await init_db()

        d = dt_date.today().isoformat() if today else date
        fills = await store.get_fills(agent_name=agent, date=d, limit=200)

        if csv_out:
            import csv, sys
            if fills:
                w = csv.DictWriter(sys.stdout, fieldnames=fills[0].keys())
                w.writeheader()
                w.writerows(fills)
        else:
            if not fills:
                console.print("[dim]No fills found.[/dim]")
                return
            for f in fills:
                console.print(
                    f"[cyan]{f.get('filled_at','')[:19]}[/cyan] "
                    f"[white]{f['symbol']:<6}[/white] "
                    f"{'[green]' if f['action']=='BUY' else '[red]'}{f['action']}[/{'green' if f['action']=='BUY' else 'red'}] "
                    f"{f['quantity']:,.0f} @ ${f['fill_price']:,.2f} "
                    f"[dim]{f.get('agent_name','')}[/dim]"
                )

    _run(_blotter())


# ── trade audit ───────────────────────────────────────────────────────────────

@app.command()
def audit(
    session: str = typer.Option(..., "--session", help="Session ID (full or first 8 chars)"),
    config: str = typer.Option("config.yaml", "--config"),
):
    """Show the full Claude session transcript for a session ID."""
    _load_config(config)

    async def _audit():
        from db.schema import init_db
        from reporting.audit_log import get_session_transcript
        await init_db()
        text = await get_session_transcript(session)
        console.print(text)

    _run(_audit())


# ── trade kill / unkill ───────────────────────────────────────────────────────

@app.command()
def kill(
    agent: Optional[str] = typer.Option(None, "--agent", help="Kill a specific agent only"),
    reason: str = typer.Option("manual", "--reason"),
    config: str = typer.Option("config.yaml", "--config"),
):
    """Activate the kill switch (halt all trading or one agent)."""
    _load_config(config)

    async def _kill():
        from db.schema import init_db
        import db.store as store
        await init_db()
        await store.set_kill_switch(active=True, agent_name=agent, reason=reason)
        scope = f"agent={agent}" if agent else "GLOBAL"
        console.print(f"[red bold]Kill switch ACTIVATED ({scope}): {reason}[/red bold]")
        try:
            from approval.telegram import send_message
            await send_message(f"🛑 *Kill switch activated* ({scope})\nReason: {reason}")
        except Exception:
            pass

    _run(_kill())


@app.command()
def unkill(
    agent: Optional[str] = typer.Option(None, "--agent"),
    config: str = typer.Option("config.yaml", "--config"),
):
    """Deactivate the kill switch."""
    _load_config(config)

    async def _unkill():
        from db.schema import init_db
        import db.store as store
        await init_db()
        await store.set_kill_switch(active=False, agent_name=agent)
        scope = f"agent={agent}" if agent else "GLOBAL"
        console.print(f"[green bold]Kill switch DEACTIVATED ({scope})[/green bold]")

    _run(_unkill())


if __name__ == "__main__":
    app()
