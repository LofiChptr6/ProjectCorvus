"""30-day agent performance chart — thin wrapper around `reporting.pnl_curve`.

Same standardized format as the hourly P&L curve: combined P&L % normalized
to opening market value of the window, buy/sell annotations, time-axis
compressed (auto-picks daily granularity for windows ≥ 7 days). Adds a
hit-rate subtitle in the title.

Outputs PNG path on stdout for the MCP wrapper.

Usage:
    python -m reporting.agent_chart --agent rex [--date 2026-04-27]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional


_SECTOR_LABEL: dict[str, str] = {
    "atlas":     "Macro / Indices / Rates / FX",
    "commodity": "Metals / Materials / Chemicals",
    "energy":    "Oil & Gas / Refiners / Services / Midstream",
    "fab":       "Semiconductor Fabs & Equipment",
    "fabless":   "Semiconductor Designers & ETFs",
    "iron":      "Industrials / Transports / Defense",
    "maya":      "Financials & Rate-Sensitive Banks",
    "rex":       "Mega-Cap Tech (Cloud/Ads/Software)",
    "trump":     "Consumer Staples & Discretionary",
    "vera":      "Healthcare / Biotech / Pharma",
    "volt":      "Utilities / REITs / Infrastructure",
}


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


async def generate(agent_name: str, chart_date: Optional[str] = None) -> str:
    """Generate the 30-day chart and return its file path."""
    repo_root = str(Path(__file__).parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    from db.schema import get_pool
    await get_pool()                                # warm pool

    from reporting.pnl_curve import render_agent_curve

    d = chart_date or date.today().isoformat()
    since = (date.fromisoformat(d) - timedelta(days=30)).isoformat()

    confirmed, total = await _fetch_hit_rate(agent_name, since)
    hit_str = (
        f"{round(100 * confirmed / total)}% hit rate ({confirmed}/{total})"
        if total else "no graded predictions"
    )
    sector = _SECTOR_LABEL.get(agent_name, agent_name)
    extra = f"{sector}  ·  {hit_str}"

    out_path = Path("data/charts") / f"{agent_name}_{d}.png"
    await render_agent_curve(
        agent_name=agent_name,
        since="30d",
        out_path=out_path,
        granularity="auto",
        extra_subtitle=extra,
    )
    return str(out_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate agent performance chart.")
    parser.add_argument("--agent", required=True, help="Agent name (e.g. rex)")
    parser.add_argument("--date", default=None, help="Chart date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    path = asyncio.run(generate(args.agent, args.date))
    print(path)
