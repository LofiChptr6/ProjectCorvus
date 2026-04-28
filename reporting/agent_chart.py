"""Generate a 30-day performance chart PNG for one sector agent.

Usage:
    python -m reporting.agent_chart --agent rex [--date 2026-04-27]

Outputs the chart path to stdout so the MCP tool can capture it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# Sector labels used in chart titles
_SECTOR_LABEL: dict[str, str] = {
    "atlas":   "Macro / Indices / Rates / FX",
    "fab":     "Semiconductor Fabs & Equipment",
    "fabless": "Semiconductor Designers & ETFs",
    "iron":    "Industrials / Transports / Defense",
    "maya":    "Financials & Rate-Sensitive Banks",
    "rex":     "Mega-Cap Tech (Cloud/Ads/Software)",
    "titan":   "Energy / Materials / Commodities",
    "trump":   "Consumer Staples & Discretionary",
    "vera":    "Healthcare / Biotech / Pharma",
    "volt":    "Utilities / REITs / Infrastructure",
}


async def _fetch_daily_pnl(agent_name: str, since: str) -> list[tuple[date, float]]:
    """Return list of (trade_date, net_pnl) sorted ascending."""
    from db.schema import get_pool
    since_dt = date.fromisoformat(since)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT decided_at::date AS trade_date,
                      COALESCE(SUM(attributed_pnl), 0)::float8 AS net_pnl
               FROM agent_pnl_attribution
               WHERE agent_name = $1
                 AND decided_at::date >= $2
                 AND attributed_pnl IS NOT NULL
               GROUP BY trade_date
               ORDER BY trade_date ASC""",
            agent_name, since_dt,
        )
    return [(row["trade_date"], float(row["net_pnl"])) for row in rows]


async def _fetch_hit_rate(agent_name: str, since: str) -> tuple[int, int]:
    """Return (confirmed, total_graded) over the window."""
    from db.schema import get_pool
    since_dt = date.fromisoformat(since)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT
                 COUNT(*) FILTER (WHERE status = 'confirmed') AS confirmed,
                 COUNT(*) FILTER (WHERE status IN ('confirmed','wrong')) AS total
               FROM agent_thesis
               WHERE agent_name = $1
                 AND resolved_at >= $2""",
            agent_name, since_dt,
        )
    if row is None:
        return 0, 0
    return int(row["confirmed"] or 0), int(row["total"] or 0)


def _build_chart(
    agent_name: str,
    chart_date: str,
    daily: list[tuple[date, float]],
    confirmed: int,
    total_graded: int,
    out_path: Path,
) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    import matplotlib.dates as mdates
    import numpy as np

    sector = _SECTOR_LABEL.get(agent_name, agent_name)
    hit_rate_pct = round(100.0 * confirmed / total_graded) if total_graded else None

    # Build full date range (fill gaps with zero)
    if daily:
        start_d = daily[0][0]
        end_d = daily[-1][0]
        date_index: list[date] = []
        d = start_d
        while d <= end_d:
            date_index.append(d)
            d += timedelta(days=1)
        pnl_map = {dt: pnl for dt, pnl in daily}
        daily_pnl = [pnl_map.get(d, 0.0) for d in date_index]
    else:
        date_index = []
        daily_pnl = []

    cumulative = list(np.cumsum(daily_pnl)) if daily_pnl else []
    total_30d = cumulative[-1] if cumulative else 0.0

    # ── Layout ────────────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(10, 6), facecolor="#1a1a2e")
    gs = gridspec.GridSpec(2, 1, height_ratios=[0.65, 0.35], hspace=0.08)
    ax_cum = fig.add_subplot(gs[0])
    ax_bar = fig.add_subplot(gs[1], sharex=ax_cum)

    for ax in (ax_cum, ax_bar):
        ax.set_facecolor("#16213e")
        ax.tick_params(colors="#aaaaaa", labelsize=8)
        ax.spines[:].set_color("#333355")

    # ── Cumulative P&L ────────────────────────────────────────────────────────
    if date_index and cumulative:
        xs = [mdates.date2num(dt) for dt in date_index]
        ys = cumulative

        ax_cum.plot(xs, ys, color="#00d4aa", linewidth=1.8, zorder=3)
        ax_cum.axhline(0, color="#555577", linewidth=0.8, zorder=2)

        # Green fill above zero, red below
        ys_arr = np.array(ys)
        ax_cum.fill_between(xs, 0, ys_arr,
                            where=ys_arr >= 0, color="#00d4aa", alpha=0.18, zorder=1)
        ax_cum.fill_between(xs, 0, ys_arr,
                            where=ys_arr < 0, color="#ff4455", alpha=0.22, zorder=1)

        ax_cum.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        plt.setp(ax_cum.get_xticklabels(), visible=False)
    else:
        ax_cum.text(0.5, 0.5, "No attribution data yet",
                    transform=ax_cum.transAxes, ha="center", va="center",
                    color="#888899", fontsize=11)

    ax_cum.set_ylabel("Cumulative P&L ($)", color="#aaaaaa", fontsize=9)
    ax_cum.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda v, _: f"${v:+,.0f}")
    )

    # ── Daily bars ────────────────────────────────────────────────────────────
    if date_index and daily_pnl:
        xs = [mdates.date2num(dt) for dt in date_index]
        colors = ["#00d4aa" if p >= 0 else "#ff4455" for p in daily_pnl]
        ax_bar.bar(xs, daily_pnl, color=colors, width=0.7, zorder=2)
        ax_bar.axhline(0, color="#555577", linewidth=0.8)
        ax_bar.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax_bar.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0))
        plt.setp(ax_bar.get_xticklabels(), rotation=30, ha="right")

    ax_bar.set_ylabel("Daily P&L ($)", color="#aaaaaa", fontsize=9)
    ax_bar.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda v, _: f"${v:+,.0f}")
    )

    # ── Title & subtitle ──────────────────────────────────────────────────────
    hit_str = f"{hit_rate_pct}% hit rate ({confirmed}/{total_graded})" if total_graded else "no predictions graded"
    subtitle = f"30d P&L: ${total_30d:+,.0f}  ·  {hit_str}"

    fig.suptitle(
        f"{agent_name.upper()} | {sector}",
        color="#e0e0ff", fontsize=13, fontweight="bold", y=0.97,
    )
    ax_cum.set_title(subtitle, color="#aaaaaa", fontsize=9, pad=4)

    # date stamp bottom-right
    fig.text(0.98, 0.01, chart_date, ha="right", va="bottom",
             color="#555577", fontsize=8)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=130, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)


async def generate(agent_name: str, chart_date: Optional[str] = None) -> str:
    """Generate chart and return the saved PNG path."""
    import sys
    import os
    # Ensure repo root is on sys.path when run as __main__
    repo_root = str(Path(__file__).parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    # Initialise DB pool
    from db.schema import get_pool
    await get_pool()  # warms the shared pool

    d = chart_date or date.today().isoformat()
    since = (date.fromisoformat(d) - timedelta(days=30)).isoformat()

    daily = await _fetch_daily_pnl(agent_name, since)
    confirmed, total_graded = await _fetch_hit_rate(agent_name, since)

    out_path = Path("data/charts") / f"{agent_name}_{d}.png"
    _build_chart(agent_name, d, daily, confirmed, total_graded, out_path)
    return str(out_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate agent performance chart.")
    parser.add_argument("--agent", required=True, help="Agent name (e.g. rex)")
    parser.add_argument("--date", default=None, help="Chart date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    path = asyncio.run(generate(args.agent, args.date))
    print(path)
