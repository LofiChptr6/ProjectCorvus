"""Hour-by-hour P&L curve renderer — per agent or desk-aggregated.

Reads `agent_state` (the hourly snapshot table) and plots cumulative
realized + unrealized + total P&L over a rolling window. Each row in
`agent_state` is one hour bucket per agent, so the curve has natural
hourly resolution.

Two render modes:
- `render_agent_curve(agent_name, ...)` — single agent's P&L curve
- `render_desk_curve(...)` — sum across all agents per hour

Both produce PNGs under `data/charts/`. Path is printed to stdout so the
MCP tool can capture it.

CLI:
    python -m reporting.pnl_curve --agent rex --since 7d
    python -m reporting.pnl_curve --desk --since all

`since` accepts ISO timestamp ('2026-05-01T00:00:00Z') or duration
string ('1d', '7d', '30d', 'all').
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# NYSE trading hours: Mon–Fri 9:30 AM – 4:00 PM Eastern (DST-aware via zoneinfo).
# We compress the X axis to only these hours so weekends + overnight gaps
# don't dominate the plot.
_NYSE_TZ = ZoneInfo("America/New_York")


def _is_trading_hour(ts: datetime) -> bool:
    et = ts.astimezone(_NYSE_TZ)
    if et.weekday() >= 5:
        return False
    minutes = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


# ── Window resolution ───────────────────────────────────────────────────────

def _resolve_since(since: str) -> Optional[datetime]:
    """Resolve a 'since' string to a UTC datetime, or None for 'all'."""
    s = (since or "").strip().lower()
    if s in ("all", "*", ""):
        return None
    if s.endswith("d") and s[:-1].isdigit():
        return datetime.now(timezone.utc) - timedelta(days=int(s[:-1]))
    if s.endswith("h") and s[:-1].isdigit():
        return datetime.now(timezone.utc) - timedelta(hours=int(s[:-1]))
    # Try ISO
    try:
        dt = datetime.fromisoformat(s.replace("z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError as e:
        raise SystemExit(f"unparseable --since: {since!r} ({e})")


# ── Fetchers ────────────────────────────────────────────────────────────────

async def _fetch_agent_curve(
    agent_name: str,
    since: Optional[datetime],
) -> list[dict]:
    """Per-snapshot rows for one agent in ascending order. Each row carries
    realized_pnl, unrealized_pnl, total_pnl, open_market_value."""
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        if since:
            rows = await conn.fetch(
                """SELECT snapshot_at,
                          realized_pnl::float8        AS r,
                          unrealized_pnl::float8      AS u,
                          total_pnl::float8           AS t,
                          open_market_value::float8   AS mv
                   FROM agent_state
                   WHERE agent_name = $1 AND snapshot_at >= $2
                   ORDER BY snapshot_at""",
                agent_name, since,
            )
        else:
            rows = await conn.fetch(
                """SELECT snapshot_at,
                          realized_pnl::float8        AS r,
                          unrealized_pnl::float8      AS u,
                          total_pnl::float8           AS t,
                          open_market_value::float8   AS mv
                   FROM agent_state
                   WHERE agent_name = $1
                   ORDER BY snapshot_at""",
                agent_name,
            )
    return [
        {"ts": r["snapshot_at"], "realized": float(r["r"]), "unrealized": float(r["u"]),
         "total": float(r["t"]), "mv": float(r["mv"])}
        for r in rows
    ]


async def _fetch_desk_curve(
    since: Optional[datetime],
) -> list[dict]:
    """Desk-aggregated rows in ascending order. Sums per hour_bucket."""
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        if since:
            rows = await conn.fetch(
                """SELECT MIN(snapshot_at) AS snapshot_at,
                          SUM(realized_pnl)::float8       AS r,
                          SUM(unrealized_pnl)::float8     AS u,
                          SUM(total_pnl)::float8          AS t,
                          SUM(open_market_value)::float8  AS mv
                   FROM agent_state
                   WHERE snapshot_at >= $1
                   GROUP BY hour_bucket
                   ORDER BY hour_bucket""",
                since,
            )
        else:
            rows = await conn.fetch(
                """SELECT MIN(snapshot_at) AS snapshot_at,
                          SUM(realized_pnl)::float8       AS r,
                          SUM(unrealized_pnl)::float8     AS u,
                          SUM(total_pnl)::float8          AS t,
                          SUM(open_market_value)::float8  AS mv
                   FROM agent_state
                   GROUP BY hour_bucket
                   ORDER BY hour_bucket"""
            )
    return [
        {"ts": r["snapshot_at"], "realized": float(r["r"]), "unrealized": float(r["u"]),
         "total": float(r["t"]), "mv": float(r["mv"])}
        for r in rows
    ]


async def _fetch_agent_fills(
    agent_name: str,
    since: Optional[datetime],
) -> list[dict]:
    """LEND/RETURN events for an agent within the window. Each row joined to
    `fills` so the annotation shows the broker-level qty (the desk's actual
    trade), not the agent's fractional share."""
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        if since:
            rows = await conn.fetch(
                """SELECT al.booked_at, UPPER(al.symbol) AS symbol, al.event,
                          al.qty::float8                  AS agent_qty,
                          al.price_per_share::float8      AS price,
                          COALESCE(f.quantity, al.qty)::float8 AS broker_qty
                   FROM agent_ledger al
                   LEFT JOIN fills f ON f.id = al.fill_id
                   WHERE al.agent_name = $1
                     AND al.booked_at >= $2
                     AND al.event IN ('LEND','RETURN')
                   ORDER BY al.booked_at""",
                agent_name, since,
            )
        else:
            rows = await conn.fetch(
                """SELECT al.booked_at, UPPER(al.symbol) AS symbol, al.event,
                          al.qty::float8                  AS agent_qty,
                          al.price_per_share::float8      AS price,
                          COALESCE(f.quantity, al.qty)::float8 AS broker_qty
                   FROM agent_ledger al
                   LEFT JOIN fills f ON f.id = al.fill_id
                   WHERE al.agent_name = $1
                     AND al.event IN ('LEND','RETURN')
                   ORDER BY al.booked_at""",
                agent_name,
            )
    return [
        {"ts": r["booked_at"], "symbol": r["symbol"], "event": r["event"],
         "agent_qty": float(r["agent_qty"]), "broker_qty": float(r["broker_qty"]),
         "price": float(r["price"])}
        for r in rows
    ]


async def _fetch_desk_fills(since: Optional[datetime]) -> list[dict]:
    """All BUY/SELL fills at the broker over the window. One row per fill
    (we de-dupe across agents by joining on fill_id distinct)."""
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        if since:
            rows = await conn.fetch(
                """SELECT filled_at AS ts, UPPER(symbol) AS symbol, action,
                          quantity::float8 AS broker_qty,
                          fill_price::float8 AS price
                   FROM fills
                   WHERE filled_at::timestamptz >= $1
                   ORDER BY filled_at""",
                since,
            )
        else:
            rows = await conn.fetch(
                """SELECT filled_at AS ts, UPPER(symbol) AS symbol, action,
                          quantity::float8 AS broker_qty,
                          fill_price::float8 AS price
                   FROM fills ORDER BY filled_at"""
            )
    out = []
    for r in rows:
        # Normalize action → LEND/RETURN style for unified rendering
        a = (r["action"] or "").upper()
        ev = "LEND" if a in ("BUY", "BOT") else "RETURN" if a in ("SELL", "SLD") else a
        ts = r["ts"]
        if isinstance(ts, str):
            from datetime import datetime
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        out.append({"ts": ts, "symbol": r["symbol"], "event": ev,
                    "agent_qty": float(r["broker_qty"]),
                    "broker_qty": float(r["broker_qty"]),
                    "price": float(r["price"])})
    return out


# ── Rendering ───────────────────────────────────────────────────────────────

def _downsample_daily(series: list[dict]) -> list[dict]:
    """Keep the last in-trading-hours snapshot of each NYSE trading day.
    Anchored on Eastern Time so the chart's day labels match the trading
    sessions exactly (no Friday-night snapshots labelled 'May 02' because
    of timezone wrap-around)."""
    by_day: dict = {}
    for s in series:
        if not _is_trading_hour(s["ts"]):
            continue                              # only count snapshots during open hours
        d = s["ts"].astimezone(_NYSE_TZ).date()
        existing = by_day.get(d)
        if existing is None or s["ts"] > existing["ts"]:
            by_day[d] = s
    return [by_day[d] for d in sorted(by_day.keys())]


def _render(
    series: list[dict],
    fills: list[dict],
    title: str,
    out_path: Path,
    *,
    granularity: str = "auto",
    extra_subtitle: Optional[str] = None,
) -> Path:
    """Standardized P&L chart with auto granularity.

    granularity: 'auto' picks daily when window >= 7 days, hourly otherwise.
                 'hourly' / 'daily' force the choice.

    - X axis is compressed:
        hourly mode → NYSE trading hours only (Mon–Fri 9:30–16:00 ET); ticks
                      every 1–4 hours (auto, scaled to span).
        daily mode  → last snapshot of each weekday only; ticks every weekday.
    - Y axis: combined P&L %, normalized so the curve starts at 0% at the
      left edge. base_value = open_market_value at the first non-zero
      snapshot in the window.
    - One blue line.
    - BUY/SELL markers labelled `BUY/SELL SYMBOL $X (Y%)`.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FixedLocator, FuncFormatter

    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not series:
        fig, ax = plt.subplots(figsize=(12, 5))
        ax.text(0.5, 0.5, "no agent_state data in window",
                ha="center", va="center", fontsize=16, color="#888")
        ax.set_axis_off()
        ax.set_title(title)
        fig.savefig(out_path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        return out_path

    if granularity == "auto":
        # Fallback if caller didn't resolve: use data span. Public API does
        # the right thing based on the requested window.
        span_seconds = (series[-1]["ts"] - series[0]["ts"]).total_seconds()
        granularity = "daily" if span_seconds >= 7 * 86400 else "hourly"

    if granularity == "daily":
        series_t = _downsample_daily(series)
        compressed = True
    else:
        series_t = [s for s in series if _is_trading_hour(s["ts"])]
        compressed = bool(series_t)
        if not series_t:
            series_t = series                     # fallback (only weekend data)

    # 2. Base value: open_market_value at the first non-zero snapshot.
    base_value = 0.0
    base_total = series_t[0]["total"]
    for s in series_t:
        if s["mv"] > 1e-6:
            base_value = s["mv"]
            base_total = s["total"]
            break
    if base_value <= 0:
        base_value = max((abs(s["mv"]) for s in series_t), default=1.0) or 1.0

    ts  = [s["ts"]    for s in series_t]
    pct = [(s["total"] - base_total) / base_value * 100.0 for s in series_t]
    n   = len(ts)
    x   = list(range(n))     # compressed integer axis

    # Taller figure when fills are dense — gives adjustText vertical room
    # to spread overlapping labels without squeezing the curve.
    fig_h = 5.0 + min(4.0, len(fills) * 0.05) if fills else 5.0
    fig, ax = plt.subplots(figsize=(13, fig_h))
    ax.axhline(0, color="black", lw=0.6, alpha=0.6, zorder=0)
    ax.plot(x, pct, lw=2.0, color="#1565c0")

    # 3. Vertical separators between trading sessions (where the gap from
    # one kept point to the next exceeds, say, 4 trading hours).
    if compressed:
        for i in range(1, n):
            if (ts[i] - ts[i - 1]).total_seconds() > 4 * 3600:
                ax.axvline(i - 0.5, color="#999", lw=0.5, ls="--", alpha=0.5)

    # 4. Last-point annotation
    last_pct = pct[-1]
    last_total_delta = pct[-1] * base_value / 100.0
    ax.annotate(
        f"{last_pct:+.2f}%\n(${last_total_delta:+,.2f})",
        xy=(x[-1], last_pct),
        xytext=(8, 0), textcoords="offset points",
        ha="left", va="center", fontsize=9,
        bbox=dict(facecolor="white", edgecolor="#999", boxstyle="round,pad=0.3", alpha=0.9),
    )

    # 5. Map a real timestamp → compressed x position by snapping to the
    # nearest kept index. Used for fill markers below and any future overlays.
    import bisect
    ts_epochs = [t.timestamp() for t in ts]
    def _x_at(t: datetime) -> Optional[int]:
        # Drop fills that aren't in a trading hour we kept; matters for the
        # rare case of after-hours print or weekend dividend.
        if compressed and not _is_trading_hour(t):
            return None
        e = t.timestamp()
        i = bisect.bisect_left(ts_epochs, e)
        if i >= n:
            i = n - 1
        elif i > 0 and (e - ts_epochs[i - 1]) < (ts_epochs[i] - e):
            i = i - 1
        return i

    def _y_at(idx: int) -> float:
        return pct[idx]

    # 6. Buy/sell markers + labels with adjustText auto-de-overlap.
    #    Aggregation key depends on granularity & density:
    #      daily            → roll up by day (per symbol+side+date)
    #      hourly, ≤25 fills → roll up by minute (per symbol+side+minute)
    #      hourly, >25 fills → roll up by symbol+side over the whole window
    #                          (desk view) so we don't paint 80 overlapping
    #                          labels. Marker is placed at the LAST fill of
    #                          each group; only the top-25 by |notional| are
    #                          drawn, the rest are silently dropped.
    if fills:
        from collections import defaultdict
        in_window = [f for f in fills if ts[0] <= f["ts"] <= ts[-1]]
        dense = (granularity == "hourly" and len(in_window) > 25)
        max_labels = 25
        agg: dict[tuple, dict] = defaultdict(
            lambda: {"notional": 0.0, "ts": None, "event": None, "symbol": None}
        )
        for f in in_window:
            ft = f["ts"]
            if granularity == "daily":
                key = (f["symbol"], f["event"],
                       ft.astimezone(_NYSE_TZ).date())
            elif dense:
                key = (f["symbol"], f["event"])
            else:
                key = (f["symbol"], f["event"],
                       ft.replace(second=0, microsecond=0))
            slot = agg[key]
            slot["notional"] += f["agent_qty"] * f["price"]
            # Keep the LAST timestamp seen for this group so the marker
            # lands on the most-recent fill within the cluster.
            if slot["ts"] is None or ft > slot["ts"]:
                slot["ts"] = ft
            slot["event"] = f["event"]
            slot["symbol"] = f["symbol"]

        # If too many groups remain, keep only the top-N by |notional|.
        groups = list(agg.values())
        if len(groups) > max_labels:
            groups = sorted(groups, key=lambda s: -abs(s["notional"]))[:max_labels]

        # First pass: plot all markers (small triangles directly on the curve).
        # Second pass: place text labels and let adjustText spread them.
        text_objs = []
        for slot in sorted(groups, key=lambda s: s["ts"]):
            xi = _x_at(slot["ts"])
            if xi is None:
                continue
            y = _y_at(xi)
            is_buy = slot["event"] == "LEND"
            marker = "^" if is_buy else "v"
            color = "#2e7d32" if is_buy else "#c62828"
            ax.plot(xi, y, marker=marker, markersize=8, color=color,
                    markeredgecolor="black", markeredgewidth=0.4, zorder=5)
            notional = slot["notional"]
            pct_of_base = (notional / base_value * 100.0) if base_value else 0.0
            label = (
                f"{'BUY' if is_buy else 'SELL'} {slot['symbol']} "
                f"${notional:,.0f} ({pct_of_base:.1f}%)"
            )
            # Initial seed position offset above (BUY) or below (SELL) the marker.
            seed_dy_pts = 14 if is_buy else -14
            seed_dy_data = seed_dy_pts * (
                (ax.get_ylim()[1] - ax.get_ylim()[0]) / fig.get_size_inches()[1] / 72
            )
            txt = ax.text(
                xi, y + seed_dy_data, label,
                fontsize=7, color=color,
                ha="center", va="bottom" if is_buy else "top",
                zorder=6,
            )
            text_objs.append(txt)

        if text_objs:
            # Pad the y-axis so labels have somewhere to go without colliding
            # with the curve. Pad based on number of fills (each tier needs
            # ~one label-height of vertical room).
            ymin, ymax = ax.get_ylim()
            yspan = ymax - ymin
            pad = max(0.25 * yspan, min(2.0 * yspan, 0.05 * len(text_objs) * yspan))
            ax.set_ylim(ymin - pad, ymax + pad)
            try:
                from adjustText import adjust_text
                adjust_text(
                    text_objs, ax=ax,
                    only_move={"text": "xy", "static": "xy", "explode": "xy"},
                    expand=(1.3, 1.4),
                    arrowprops=dict(arrowstyle="-", color="#888", alpha=0.45, lw=0.6),
                    force_text=(0.5, 1.1),
                    force_static=(0.3, 0.5),
                    max_move=200,
                )
            except ImportError:
                pass

    # 7. X-axis tick labels.
    #    daily mode  → tick at every weekday (or every Nth if too many).
    #    hourly mode → tick at EVERY trading hour (cap at ~50 ticks then thin).
    if n > 1:
        if granularity == "daily":
            tick_idxs = list(range(n))
            if n > 12:
                step = max(1, n // 10)
                tick_idxs = list(range(0, n, step))
                if tick_idxs[-1] != n - 1:
                    tick_idxs.append(n - 1)
            labels = [ts[i].astimezone(_NYSE_TZ).strftime("%b %d") for i in tick_idxs]
        else:
            # One tick per kept (= trading-hour) data point. For very wide
            # windows we thin to keep ~50 ticks max so labels stay legible
            # even when rotated.
            if n <= 60:
                tick_idxs = list(range(n))
            else:
                step = max(1, n // 50)
                tick_idxs = list(range(0, n, step))
                if tick_idxs[-1] != n - 1:
                    tick_idxs.append(n - 1)
            labels = []
            last_date = None
            for ti in tick_idxs:
                et = ts[ti].astimezone(_NYSE_TZ)
                d = et.strftime("%m-%d")
                # Single-line labels read better when rotated 90°.
                label = (f"{d} {et.strftime('%H:%M')} ET"
                         if d != last_date else et.strftime("%H:%M ET"))
                labels.append(label)
                last_date = d
        idx_to_label = dict(zip(tick_idxs, labels))
        ax.xaxis.set_major_locator(FixedLocator(tick_idxs))
        ax.xaxis.set_major_formatter(FuncFormatter(
            lambda val, pos: idx_to_label.get(int(val), "")
        ))

    if granularity == "daily":
        gran_note = "weekdays only (last in-hours snapshot per ET-day)"
    elif compressed:
        gran_note = "trading hours only (Mon–Fri 9:30–16:00 ET)"
    else:
        gran_note = "raw (no trading-hour data in window)"
    subtitle = f"{gran_note}; base = ${base_value:,.2f}"
    if extra_subtitle:
        subtitle = f"{subtitle}  ·  {extra_subtitle}"
    ax.set_title(f"{title}\n{subtitle}")
    ax.set_ylabel("Combined P&L (%)")
    ax.grid(True, alpha=0.25)
    # Vertical (90°) labels prevent crowding when there's one tick per hour.
    rotation = 90 if granularity == "hourly" else 30
    plt.setp(ax.get_xticklabels(), rotation=rotation, fontsize=7,
             ha=("center" if rotation == 90 else "right"))

    fig.tight_layout()
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return out_path


# ── Public API ──────────────────────────────────────────────────────────────

def _auto_granularity(since: str) -> str:
    """Hourly for windows up to 7 days (every trading hour gets a tick).
    Daily for windows wider than a week, 'all', or ISO timestamps older
    than ~7 days. Parsed off the `since` string directly so 'Nd' tokens
    don't suffer microsecond drift between two `now()` calls."""
    s = (since or "").strip().lower()
    if s in ("", "all", "*"):
        return "daily"
    if s.endswith("h") and s[:-1].isdigit():
        return "hourly"
    if s.endswith("d") and s[:-1].isdigit():
        return "hourly" if int(s[:-1]) <= 7 else "daily"
    # ISO timestamp fallback (rare): use span check with 1-hour slack.
    try:
        dt = datetime.fromisoformat(s.replace("z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        span = (datetime.now(timezone.utc) - dt).total_seconds()
        return "hourly" if span <= 7 * 86400 + 3600 else "daily"
    except ValueError:
        return "hourly"


async def render_agent_curve(
    agent_name: str,
    since: str = "7d",
    out_path: Optional[Path] = None,
    *,
    granularity: str = "auto",
    extra_subtitle: Optional[str] = None,
) -> Path:
    """Render one agent's standardized P&L% curve with buy/sell annotations.
    Returns the saved PNG path. `granularity` is 'auto' (default), 'hourly',
    or 'daily'."""
    if out_path is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = Path("data/charts") / f"pnl_curve_{agent_name}_{stamp}.png"
    s = _resolve_since(since)
    series = await _fetch_agent_curve(agent_name, s)
    fills  = await _fetch_agent_fills(agent_name, s)
    title  = f"{agent_name} — combined P&L % (since {since})"
    if granularity == "auto":
        granularity = _auto_granularity(since)
    return _render(series, fills, title, out_path,
                   granularity=granularity, extra_subtitle=extra_subtitle)


async def render_desk_curve(
    since: str = "7d",
    out_path: Optional[Path] = None,
    *,
    granularity: str = "auto",
    extra_subtitle: Optional[str] = None,
) -> Path:
    """Render the desk-aggregated standardized P&L% curve with broker fills."""
    if out_path is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = Path("data/charts") / f"pnl_curve_desk_{stamp}.png"
    s = _resolve_since(since)
    series = await _fetch_desk_curve(s)
    fills  = await _fetch_desk_fills(s)
    title  = f"DESK — combined P&L % (since {since})"
    if granularity == "auto":
        granularity = _auto_granularity(since)
    return _render(series, fills, title, out_path,
                   granularity=granularity, extra_subtitle=extra_subtitle)


# ── CLI ────────────────────────────────────────────────────────────────────

async def _main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--agent", help="agent name (e.g. rex)")
    g.add_argument("--desk", action="store_true", help="desk-aggregated curve")
    p.add_argument("--since", default="7d", help="ISO timestamp or duration (1d/7d/30d/all)")
    p.add_argument("--out", help="output path (default: data/charts/...)")
    args = p.parse_args()

    out = Path(args.out) if args.out else None
    if args.desk:
        path = await render_desk_curve(since=args.since, out_path=out)
    else:
        path = await render_agent_curve(args.agent, since=args.since, out_path=out)
    print(str(path))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
