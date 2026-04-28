"""Render the Holding Kanban as a stacked colored bar chart.

Usage:
    python -m reporting.kanban_chart [--date 2026-04-27]

Builds the latest snapshot tick on `date` (defaults to today). Each bar is
one symbol; segments are coloured by agent; height is market_value (USD).
Saves PNG under data/charts/ and prints the path to stdout so the MCP tool
can capture it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date as _date
from pathlib import Path
from typing import Optional


# Stable per-agent colors, matching the dark theme used by agent_chart.py.
_AGENT_COLORS: dict[str, str] = {
    "atlas":     "#00d4aa",
    "fab":       "#ffaa44",
    "fabless":   "#ff7755",
    "iron":      "#88aaff",
    "maya":      "#ee5599",
    "rex":       "#aa88ff",
    "titan":     "#ddbb44",
    "trump":     "#dd6633",
    "vera":      "#44ddbb",
    "volt":      "#66bbff",
    "__orphan__": "#666688",
}

_DEFAULT_COLOR = "#999999"


async def _fetch_latest_tick(on_date: str) -> tuple[Optional[str], list[dict]]:
    """Most recent snapshot tick on `on_date` (or earlier if no data on that day).
    Returns (snapshot_at_iso, [rows...])."""
    from datetime import datetime, timezone
    from db.schema import get_pool
    pool = await get_pool()
    # End-of-day in UTC; the snapshot_at column is timestamptz so as long as
    # this bound is monotonically later than any same-day tick we're fine.
    eod = datetime.fromisoformat(on_date).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    async with pool.acquire() as conn:
        tick = await conn.fetchval(
            """SELECT MAX(snapshot_at) FROM holding_kanban
               WHERE snapshot_at <= $1""",
            eod,
        )
        if tick is None:
            return None, []
        rows = await conn.fetch(
            """SELECT agent_name, symbol, holding_qty, market_value,
                      conviction, direction, attribution_share,
                      desk_nav, snapshot_at
               FROM holding_kanban
               WHERE snapshot_at = $1
               ORDER BY symbol, agent_name""",
            tick,
        )
    return str(tick), [dict(r) for r in rows]


def _build_chart(
    snapshot_at: Optional[str],
    rows: list[dict],
    out_path: Path,
) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    fig = plt.figure(figsize=(12, 6.5), facecolor="#1a1a2e")
    ax = fig.add_subplot(1, 1, 1)
    ax.set_facecolor("#16213e")
    ax.tick_params(colors="#aaaaaa", labelsize=9)
    for s in ax.spines.values():
        s.set_color("#333355")

    if not rows:
        ax.text(0.5, 0.5, "No Holding Kanban data yet",
                transform=ax.transAxes, ha="center", va="center",
                color="#888899", fontsize=12)
        ax.set_xticks([]); ax.set_yticks([])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(out_path), dpi=130, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        return

    # Group market_value by symbol → agent. Symbols sorted by total |MV|
    # so the largest stack is on the left.
    by_sym: dict[str, dict[str, float]] = {}
    for r in rows:
        sym = r["symbol"]
        agent = r["agent_name"]
        mv = float(r.get("market_value") or 0.0)
        by_sym.setdefault(sym, {})
        by_sym[sym][agent] = by_sym[sym].get(agent, 0.0) + mv

    sym_totals = {s: sum(abs(v) for v in m.values()) for s, m in by_sym.items()}
    symbols = sorted(by_sym.keys(), key=lambda s: sym_totals[s], reverse=True)
    # Cap at 25 symbols for readability.
    if len(symbols) > 25:
        symbols = symbols[:25]

    # Stable agent ordering (alphabetical, orphan last).
    all_agents = sorted({r["agent_name"] for r in rows})
    if "__orphan__" in all_agents:
        all_agents.remove("__orphan__"); all_agents.append("__orphan__")

    x = np.arange(len(symbols))
    bottoms_pos = np.zeros(len(symbols))
    bottoms_neg = np.zeros(len(symbols))
    legend_handles: dict[str, object] = {}

    for agent in all_agents:
        color = _AGENT_COLORS.get(agent, _DEFAULT_COLOR)
        values = np.array([by_sym[s].get(agent, 0.0) for s in symbols])
        if not values.any():
            continue
        # Stack positives upward and negatives downward separately so signs are visible.
        pos = np.where(values > 0, values, 0.0)
        neg = np.where(values < 0, values, 0.0)
        if pos.any():
            bar = ax.bar(x, pos, bottom=bottoms_pos, color=color, edgecolor="#1a1a2e",
                         linewidth=0.5, label=agent, width=0.7)
            bottoms_pos += pos
            legend_handles.setdefault(agent, bar)
        if neg.any():
            bar = ax.bar(x, neg, bottom=bottoms_neg, color=color, edgecolor="#1a1a2e",
                         linewidth=0.5, hatch="///", width=0.7)
            bottoms_neg += neg
            legend_handles.setdefault(agent, bar)

    ax.axhline(0, color="#555577", linewidth=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(symbols, rotation=40, ha="right", color="#dddddd", fontsize=9)
    ax.set_ylabel("Market value ($)", color="#aaaaaa", fontsize=10)
    ax.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda v, _: f"${v:+,.0f}")
    )

    desk_nav = float(rows[0].get("desk_nav") or 0.0) if rows else 0.0
    title = f"Holding Kanban — desk NAV ${desk_nav:,.0f}"
    fig.suptitle(title, color="#e0e0ff", fontsize=13, fontweight="bold", y=0.97)
    sub = f"snapshot {snapshot_at}" if snapshot_at else ""
    ax.set_title(sub, color="#aaaaaa", fontsize=9, pad=4)

    if legend_handles:
        leg = ax.legend(
            legend_handles.values(), legend_handles.keys(),
            loc="upper right", fontsize=8, ncol=2,
            facecolor="#16213e", edgecolor="#333355", labelcolor="#dddddd",
        )
        for t in leg.get_texts():
            t.set_color("#dddddd")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=130, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)


async def _fetch_hourly(on_date: str) -> list[dict]:
    """Every (snapshot_at, symbol) row on `on_date`, summed across agents.
    Also annotates each row with `dominant_agent` — the agent that owns
    the largest cumulative attribution share for that symbol today (used
    to assign the symbol a color in the dominant agent's family)."""
    from datetime import datetime, timezone
    from db.schema import get_pool
    pool = await get_pool()
    start = datetime.fromisoformat(on_date).replace(tzinfo=timezone.utc)
    end = start.replace(hour=23, minute=59, second=59)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT snapshot_at, symbol,
                      SUM(market_value)::float8 AS market_value,
                      SUM(holding_qty)::float8 AS holding_qty
               FROM holding_kanban
               WHERE snapshot_at BETWEEN $1 AND $2
               GROUP BY snapshot_at, symbol
               ORDER BY snapshot_at, symbol""",
            start, end,
        )
        # Dominant agent per symbol = the one with the largest cumulative
        # |market_value| share across the day.
        dom = await conn.fetch(
            """SELECT symbol, agent_name,
                      SUM(ABS(market_value))::float8 AS w
               FROM holding_kanban
               WHERE snapshot_at BETWEEN $1 AND $2
               GROUP BY symbol, agent_name""",
            start, end,
        )
    dom_map: dict[str, str] = {}
    pool_score: dict[str, dict[str, float]] = {}
    for d in dom:
        pool_score.setdefault(d["symbol"], {})[d["agent_name"]] = float(d["w"])
    for sym, scores in pool_score.items():
        dom_map[sym] = max(scores.items(), key=lambda kv: kv[1])[0]
    out = []
    for r in rows:
        sym = r["symbol"]
        out.append({
            "snapshot_at": r["snapshot_at"],
            "symbol": sym,
            "market_value": float(r["market_value"] or 0),
            "holding_qty": float(r["holding_qty"] or 0),
            "dominant_agent": dom_map.get(sym, "__orphan__"),
        })
    return out


def _color_variants(base_hex: str, n: int) -> list[str]:
    """Return n visually-distinct shades of base_hex by varying HLS lightness.
    Keeps hue constant so all variants read as 'same family'."""
    import colorsys
    h = base_hex.lstrip("#")
    r, g, b = int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255
    base_h, base_l, base_s = colorsys.rgb_to_hls(r, g, b)
    if n <= 1:
        return [base_hex]
    # Spread lightness over [base_l-0.18, base_l+0.18], clamped to [0.25, 0.85].
    spread = 0.18
    lo = max(0.25, base_l - spread)
    hi = min(0.85, base_l + spread)
    out = []
    for i in range(n):
        t = i / (n - 1)
        l = lo + t * (hi - lo)
        nr, ng, nb = colorsys.hls_to_rgb(base_h, l, base_s)
        out.append("#{:02x}{:02x}{:02x}".format(int(nr*255), int(ng*255), int(nb*255)))
    return out


def _build_hourly_chart(rows: list[dict], chart_date: str, out_path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import numpy as np

    fig = plt.figure(figsize=(13, 7), facecolor="#1a1a2e")
    ax = fig.add_subplot(1, 1, 1)
    ax.set_facecolor("#16213e")
    ax.tick_params(colors="#aaaaaa", labelsize=9)
    for s in ax.spines.values():
        s.set_color("#333355")

    if not rows:
        ax.text(0.5, 0.5, f"No Holding Kanban data for {chart_date}",
                transform=ax.transAxes, ha="center", va="center",
                color="#888899", fontsize=12)
        ax.set_xticks([]); ax.set_yticks([])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(out_path), dpi=130, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        return

    # Pivot: ticks (x) × symbols → market_value
    ticks = sorted({r["snapshot_at"] for r in rows})
    # Order symbols by dominant agent then by total |MV| (so same-agent
    # symbols cluster in the legend and stack adjacently).
    sym_total: dict[str, float] = {}
    sym_agent: dict[str, str] = {}
    for r in rows:
        sym_total[r["symbol"]] = sym_total.get(r["symbol"], 0.0) + abs(r["market_value"])
        sym_agent[r["symbol"]] = r["dominant_agent"]
    symbols = sorted(
        sym_total.keys(),
        key=lambda s: (sym_agent[s], -sym_total[s]),
    )

    # Per-symbol color: variant within its dominant agent's family.
    by_agent: dict[str, list[str]] = {}
    for s in symbols:
        by_agent.setdefault(sym_agent[s], []).append(s)
    sym_color: dict[str, str] = {}
    for agent, syms in by_agent.items():
        base = _AGENT_COLORS.get(agent, _DEFAULT_COLOR)
        variants = _color_variants(base, len(syms))
        for s, c in zip(syms, variants):
            sym_color[s] = c

    # Build the matrix
    mv = {(r["snapshot_at"], r["symbol"]): r["market_value"] for r in rows}
    x_nums = mdates.date2num(ticks)
    # Bar width: ~80% of median tick gap in days (matplotlib date units).
    if len(x_nums) > 1:
        gaps = np.diff(x_nums)
        width = float(np.median(gaps)) * 0.8
    else:
        width = 0.03  # ~45 min

    bottoms_pos = np.zeros(len(ticks))
    bottoms_neg = np.zeros(len(ticks))

    for sym in symbols:
        vals = np.array([mv.get((t, sym), 0.0) for t in ticks])
        if not vals.any():
            continue
        pos = np.where(vals > 0, vals, 0.0)
        neg = np.where(vals < 0, vals, 0.0)
        c = sym_color[sym]
        if pos.any():
            ax.bar(x_nums, pos, bottom=bottoms_pos, width=width,
                   color=c, edgecolor="#1a1a2e", linewidth=0.3, label=f"{sym} ({sym_agent[sym]})")
            bottoms_pos += pos
        if neg.any():
            ax.bar(x_nums, neg, bottom=bottoms_neg, width=width,
                   color=c, edgecolor="#1a1a2e", linewidth=0.3, hatch="///")
            bottoms_neg += neg

    # Format x-axis as time-of-day.
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=ticks[0].tzinfo))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=12))
    plt.setp(ax.get_xticklabels(), color="#dddddd")

    ax.axhline(0, color="#555577", linewidth=0.8)
    ax.set_ylabel("Equity ($)", color="#aaaaaa", fontsize=10)
    ax.set_xlabel("UTC time", color="#aaaaaa", fontsize=10)
    ax.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda v, _: f"${v:+,.0f}")
    )

    fig.suptitle(f"Holding Kanban — hourly trajectory  ·  {chart_date}",
                 color="#e0e0ff", fontsize=13, fontweight="bold", y=0.97)
    ax.set_title("each segment = one ticker; colored by managing agent's family",
                 color="#aaaaaa", fontsize=9, pad=4)

    # Legend: too many tickers for one column; show in 3-4 columns at right.
    ncol = 4 if len(symbols) > 18 else (3 if len(symbols) > 9 else 2)
    leg = ax.legend(
        loc="center left", bbox_to_anchor=(1.01, 0.5),
        fontsize=7, ncol=ncol, frameon=True,
        facecolor="#16213e", edgecolor="#333355",
    )
    for t in leg.get_texts():
        t.set_color("#dddddd")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=130, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)


async def generate(chart_date: Optional[str] = None, mode: str = "snapshot") -> str:
    """`mode` is 'snapshot' (latest tick stacked bars) or 'hourly'
    (x = time, stacked = tickers, colored by managing agent)."""
    repo_root = str(Path(__file__).parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from db.schema import get_pool
    await get_pool()

    d = chart_date or _date.today().isoformat()
    if mode == "hourly":
        rows = await _fetch_hourly(d)
        out_path = Path("data/charts") / f"kanban_hourly_{d}.png"
        _build_hourly_chart(rows, d, out_path)
    else:
        snapshot_at, rows = await _fetch_latest_tick(d)
        out_path = Path("data/charts") / f"kanban_{d}.png"
        _build_chart(snapshot_at, rows, out_path)
    return str(out_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Render the Holding Kanban.")
    parser.add_argument("--date", default=None, help="Chart date YYYY-MM-DD (default: today)")
    parser.add_argument("--mode", default="snapshot", choices=["snapshot", "hourly"],
                        help="snapshot: latest tick stacked bars; hourly: time × tickers stack")
    args = parser.parse_args()
    path = asyncio.run(generate(args.date, mode=args.mode))
    print(path)
