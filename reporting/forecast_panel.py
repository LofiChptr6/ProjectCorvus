"""Per-agent forecast panel renderer.

Top-N tickers (default 10), one full-width row per ticker, stacked vertically.

Each row uses the same compressed-trading-hour x-axis design as
`reporting/pnl_curve.py`:
  - Past portion: hourly closes over the last 5 trading days. NYSE hours
    only (Mon–Fri 9:30–16:00 ET); overnight + weekend gaps are squeezed
    out. X axis is a kept-trading-hour integer index, formatted back to
    real timestamps for ticks.
  - Forecast portion: a dashed line from today's price out to
    `time_to_target_days` trading days, ending at
        today × (1 + expected_return_pct/100 × conviction)
    (sign flipped for direction='short'). End-point pill-annotated with
    the horizon length so the agent's timescale is legible at a glance.
  - Day separators: vertical dotted lines wherever consecutive kept
    points span > 4 trading hours (i.e. an overnight gap), plus a thicker
    "today" separator between past and forecast.

Top-N selection (decided 2026-05-03):
  Union of (active convictions) ∪ (current positions from latest
  agent_state snapshot). Dedup by symbol. Sort descending by abs(market
  value) then abs(conviction). Take 10.

Output: data/charts/forecast_{agent}_{YYYYMMDD_HHMMSS}.png

CLI:
    python -m reporting.forecast_panel --agent fab
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_OUT_DIR = Path("data/charts")
# 7 trading days fetched so RSI_14 (14-hour warmup) has all-valid values
# across the full displayed window. The first ~2 days are warmup-NaN at
# the start; chart still shows the whole 7-day past for context.
_PAST_TRADING_DAYS = 7
_MAX_TICKERS = 10
_FORECAST_RETURN_CAP = 5.0           # clamp |projected return| to ±500%
_NYSE_TZ = ZoneInfo("America/New_York")
_TRADING_HOURS_PER_DAY = 7           # 9:30, 10:30, …, 15:30 = 7 hourly bars
_MAX_TICK_LABELS = 80                # cap on labelled ticks per row


# ── Trading-hour helpers ─────────────────────────────────────────────────────

def _is_trading_hour(ts: datetime) -> bool:
    """True iff ts falls inside a NYSE regular session (Mon–Fri 9:30–16:00 ET).
    Mirrors the helper in reporting/pnl_curve.py so both charts compress time
    the same way."""
    et = ts.astimezone(_NYSE_TZ)
    if et.weekday() >= 5:
        return False
    minutes = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


def _project_trading_day(today_et: datetime, n_days: int) -> datetime:
    """Walk forward `n_days` weekdays from `today_et` (an ET date), returning
    the close (16:00 ET) of the n-th trading day. Approximation — does not
    account for market holidays. Plenty for chart-axis labels."""
    cur = today_et
    skipped = 0
    while skipped < n_days:
        cur = cur + timedelta(days=1)
        if cur.weekday() < 5:
            skipped += 1
    return cur.replace(hour=16, minute=0, second=0, microsecond=0)


def _project_trading_hour(today_et: datetime, k: int) -> datetime:
    """Return the ET datetime k trading hours after ``today_et``. Trading
    hours are 9:30, 10:30, 11:30, 12:30, 13:30, 14:30, 15:30 ET (7 per
    weekday). Holidays are not modelled — chart-axis approximation."""
    if k <= 0:
        return today_et
    days_forward, hour_in_day = divmod(k, _TRADING_HOURS_PER_DAY)
    target_day = _project_trading_day(today_et, days_forward) if days_forward else today_et
    return target_day.replace(hour=9 + hour_in_day, minute=30,
                              second=0, microsecond=0)


# ── Selection ────────────────────────────────────────────────────────────────

def _load_agent_universe(agent_name: str) -> list[str]:
    """Return the agent's full sector-universe symbol list in
    sector_map.yaml order (insertion order). Empty if the agent or the file
    is missing."""
    import yaml as _yaml
    p = Path("agents/sector_map.yaml")
    if not p.exists():
        return []
    try:
        sm = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    universe = (
        ((sm.get("agents") or {}).get(agent_name, {})).get("universe") or {}
    )
    return [str(s).upper() for s in universe.keys()]


def _load_agent_indicators(agent_name: str) -> dict:
    """Return the agent's `indicators:` block from agents/<agent>.yaml.
    Falls back to the default SMA50/200 + RSI14 + BBANDS20 set if the
    block is missing (for legacy personas)."""
    import yaml as _yaml
    p = Path(f"agents/{agent_name}.yaml")
    spec: dict = {}
    if p.exists():
        try:
            cfg = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            spec = cfg.get("indicators") or {}
        except Exception as exc:
            log.warning("forecast_panel: failed to read %s: %s", p, exc)
    spec.setdefault("trend", ["SMA_50", "SMA_200"])
    spec.setdefault("oscillator", "RSI_14")
    spec.setdefault("envelope", "BBANDS_20")
    spec.setdefault("overbought", 70)
    spec.setdefault("oversold", 30)
    return spec


async def _select_top_tickers(agent_name: str) -> list[dict]:
    """Hybrid selection — up to _MAX_TICKERS rows:
      1. Active forecasts (the agent's published research, ≥20 per hour).
      2. Current positions (latest agent_state snapshot).
      3. Sector-universe filler in sector_map.yaml insertion order until
         we reach the cap. Universe-only rows have no position and no
         forecast, so they render past-5-day prices only.
    Convictions are layered on top to flag rows where the agent moved
    from "view" to "act" (✓momentum / ⚠early markers).
    Final rows sort by abs(market_value) then abs(forecast_score)."""
    from db import store

    by_sym: dict[str, dict] = {}
    # horizon rows per symbol: {sym: {horizon: forecast_dict}}
    horizon_rows: dict[str, dict[str, dict]] = {}

    # 1. Forecasts — the new primary source. Each row carries the inputs
    #    for one dashed forecast line on the chart. Multi-horizon: up to 4
    #    rows per symbol (intraday/near/far/cycle). All are stored; the chart
    #    renders one dashed line per horizon.
    forecasts = await store.get_agent_active_forecasts(agent_name)
    for f in forecasts:
        sym = (f.get("symbol") or "").upper()
        if not sym:
            continue
        er = (
            float(f["expected_return_pct"])
            if f.get("expected_return_pct") is not None else None
        )
        lk = (
            float(f["likelihood"])
            if f.get("likelihood") is not None else None
        )
        ttd = (
            int(f["time_to_target_days"])
            if f.get("time_to_target_days") is not None else None
        )
        score = (
            float(f["forecast_score"])
            if f.get("forecast_score") is not None else None
        )
        horizon = str(f.get("horizon") or "intraday")
        # Store all horizons for this symbol
        horizon_rows.setdefault(sym, {})[horizon] = {
            "expected_return_pct": er,
            "likelihood": lk,
            "time_to_target_days": ttd,
            "forecast_score": score,
            "method": f.get("method"),
            "rationale": f.get("rationale"),
            "horizon": horizon,
        }
        # Primary display row: prefer intraday, fall back to nearest horizon
        existing = by_sym.get(sym)
        _HORIZON_ORDER = ("intraday", "near", "far", "cycle")
        use_as_primary = (
            existing is None
            or _HORIZON_ORDER.index(horizon) <
               _HORIZON_ORDER.index(existing.get("_primary_horizon", "cycle"))
        )
        if use_as_primary:
            by_sym[sym] = {
                "symbol": sym,
                "expected_return_pct": er,
                "likelihood": lk,
                "time_to_target_days": ttd,
                "forecast_score": score,
                "method": f.get("method"),
                "rationale": f.get("rationale"),
                "_primary_horizon": horizon,
                # populated below if a matching conviction exists
                "conviction": 0.0,
                "direction": None,
                "momentum_confirmed": None,
                "position_qty": 0.0,
                "market_value": 0.0,
                "avg_cost": None,
                "mark": None,
            }

    # 2. Convictions — overlay on the forecast row if it exists; else add.
    #    The chart uses the conviction's direction + momentum_confirmed
    #    flag for badge display (✓momentum / ⚠early), keeping forecast
    #    inputs (er × likelihood × horizon) as the line driver.
    convictions = await store.get_agent_active_convictions(agent_name)
    for c in convictions:
        sym = (c.get("symbol") or "").upper()
        if not sym:
            continue
        entry = by_sym.get(sym) or {
            "symbol": sym,
            "expected_return_pct": (
                float(c["expected_return_pct"])
                if c.get("expected_return_pct") is not None else None
            ),
            "likelihood": None,
            "time_to_target_days": (
                int(c["time_to_target_days"])
                if c.get("time_to_target_days") is not None else None
            ),
            "forecast_score": None,
            "method": "conviction (no forecast row)",
            "rationale": c.get("rationale"),
            "position_qty": 0.0,
            "market_value": 0.0,
            "avg_cost": None,
            "mark": None,
        }
        entry["conviction"] = float(c.get("conviction") or 0.0)
        entry["direction"] = c.get("direction")
        entry["momentum_confirmed"] = c.get("momentum_confirmed")
        by_sym[sym] = entry

    snapshots = await store.get_agent_state_history(agent_name, lookback_hours=48)
    if snapshots:
        latest = snapshots[0]
        positions = latest.get("positions_json") or []
        if isinstance(positions, str):
            try:
                positions = json.loads(positions)
            except (TypeError, ValueError):
                positions = []
        for p in positions or []:
            sym = (p.get("sym") or p.get("symbol") or "").upper()
            if not sym:
                continue
            qty = float(p.get("qty") or p.get("quantity") or 0.0)
            if qty == 0:
                continue
            mv = float(p.get("market_value") or 0.0)
            entry = by_sym.get(sym) or {
                "symbol": sym, "conviction": 0.0, "direction": None,
                "expected_return_pct": None, "time_to_target_days": None,
                "rationale": None, "momentum_confirmed": None,
            }
            entry["position_qty"] = qty
            entry["market_value"] = mv
            entry["avg_cost"] = float(p["avg_cost"]) if p.get("avg_cost") is not None else None
            entry["mark"] = float(p["mark"]) if p.get("mark") is not None else None
            by_sym[sym] = entry

    # Universe filler — only if we still have empty slots after conviction +
    # position rows. Rows are added in sector_map order. They carry no
    # position and no forecast (both blank), so the rendered row shows just
    # the past-5-day price line.
    if len(by_sym) < _MAX_TICKERS:
        for sym in _load_agent_universe(agent_name):
            if sym in by_sym:
                continue
            by_sym[sym] = {
                "symbol": sym,
                "conviction": 0.0,
                "direction": None,
                "expected_return_pct": None,
                "time_to_target_days": None,
                "rationale": None,
                "momentum_confirmed": None,
                "position_qty": 0.0,
                "market_value": 0.0,
                "avg_cost": None,
                "mark": None,
                "_universe_only": True,
            }
            if len(by_sym) >= _MAX_TICKERS:
                break

    # Attach all horizon rows to each symbol entry so the renderer can draw
    # one dashed line per horizon (intraday solid, near/far/cycle progressively
    # more transparent and thinner).
    for sym, entry in by_sym.items():
        entry["forecast_horizons"] = list((horizon_rows.get(sym) or {}).values())

    rows = list(by_sym.values())
    rows.sort(
        key=lambda r: (abs(r.get("market_value") or 0.0),
                       abs(r.get("forecast_score") or 0.0),
                       abs(r.get("conviction") or 0.0)),
        reverse=True,
    )
    return rows[:_MAX_TICKERS]


# ── Per-symbol fills (LEND/RETURN) ───────────────────────────────────────────

async def _fetch_recent_fills(agent_name: str, symbols: set[str],
                              since: datetime) -> dict[str, list[dict]]:
    """LEND/RETURN events from agent_ledger for one agent across the given
    symbols since `since`. Returned as {SYMBOL: [{ts, event, qty, price}, …]}
    sorted ascending. Joined to `fills` so the marker reflects the broker
    quantity rather than the fractional agent share."""
    if not symbols:
        return {}
    from db.schema import get_pool
    pool = await get_pool()
    syms_upper = [s.upper() for s in symbols]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT al.booked_at, UPPER(al.symbol) AS symbol, al.event,
                      al.qty::float8                  AS agent_qty,
                      al.price_per_share::float8      AS price,
                      COALESCE(f.quantity, al.qty)::float8 AS broker_qty
               FROM agent_ledger al
               LEFT JOIN fills f ON f.id = al.fill_id
               WHERE al.agent_name = $1
                 AND al.symbol = ANY($2::text[])
                 AND al.booked_at >= $3
                 AND al.event IN ('LEND','RETURN')
               ORDER BY al.booked_at""",
            agent_name, syms_upper, since,
        )
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["symbol"], []).append({
            "ts": r["booked_at"],
            "event": r["event"],
            "agent_qty": float(r["agent_qty"]),
            "broker_qty": float(r["broker_qty"]),
            "price": float(r["price"]),
        })
    return out


# ── Bars ─────────────────────────────────────────────────────────────────────

async def _fetch_hourly_closes(symbol: str, n_days: int = _PAST_TRADING_DAYS) -> list[dict]:
    """Hourly close bars for the past ``n_days`` trading days, filtered to
    NYSE trading hours only. Asks for ``n_days + 4`` calendar days to cover
    weekends + holidays, then keeps the in-hours bars from the requested
    span. Returns [{t, c}, ...] ascending."""
    from data.massive_client import get_bars
    duration = f"{max(n_days + 4, 8)} D"
    try:
        resp = await get_bars(symbol, bar_size="1 hour", duration=duration)
    except Exception as exc:
        log.warning("forecast_panel: get_bars(%s) failed: %s", symbol, exc)
        return []
    bars = (resp or {}).get("bars") or []
    cleaned = []
    for b in bars:
        t = b.get("t")
        c = b.get("c") if b.get("c") is not None else b.get("close")
        if t is None or c is None:
            continue
        try:
            ts = _coerce_datetime(t)
        except (TypeError, ValueError):
            continue
        if not _is_trading_hour(ts):
            continue
        try:
            cleaned.append({"t": ts, "c": float(c)})
        except (TypeError, ValueError):
            continue
    cleaned.sort(key=lambda x: x["t"])
    # Keep only bars from the last n_days *trading* days. Walk back from the
    # newest bar's ET-date n_days weekdays.
    if not cleaned:
        return []
    newest_et_date = cleaned[-1]["t"].astimezone(_NYSE_TZ).date()
    cutoff_date = newest_et_date
    days_back = 0
    while days_back < n_days - 1:
        cutoff_date = cutoff_date - timedelta(days=1)
        if cutoff_date.weekday() < 5:
            days_back += 1
    return [b for b in cleaned if b["t"].astimezone(_NYSE_TZ).date() >= cutoff_date]


async def _fetch_daily_closes(symbol: str, n_days: int = 300) -> list[float]:
    """Daily close array, oldest→newest. Used to compute long-window
    indicators (SMA_200, BBANDS_20) without dragging hourly bars across
    a full year. Asks Massive for ``n_days`` calendar days; returns just
    the close numbers since indicators don't need timestamps once the
    series is contiguous."""
    from data.massive_client import get_bars
    duration = f"{max(n_days, 50)} D"
    try:
        resp = await get_bars(symbol, bar_size="1 day", duration=duration)
    except Exception as exc:
        log.warning("forecast_panel: get_bars(%s daily %s) failed: %s",
                    symbol, duration, exc)
        return []
    bars = (resp or {}).get("bars") or []
    cleaned: list[tuple[datetime, float]] = []
    for b in bars:
        t = b.get("t")
        c = b.get("c") if b.get("c") is not None else b.get("close")
        if t is None or c is None:
            continue
        try:
            ts = _coerce_datetime(t)
            cleaned.append((ts, float(c)))
        except (TypeError, ValueError):
            continue
    cleaned.sort(key=lambda x: x[0])
    return [c for _, c in cleaned]


def _coerce_datetime(t) -> datetime:
    if isinstance(t, datetime):
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(t / 1000.0 if t > 1e12 else t, tz=timezone.utc)
    if isinstance(t, str):
        s = t.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return datetime.strptime(s.split(".")[0], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    raise ValueError(f"unparseable bar time: {t!r}")


# ── Forecast endpoint ────────────────────────────────────────────────────────

def _forecast_endpoint(today_price: float, entry: dict) -> Optional[tuple[float, int]]:
    """Compute (end_price, horizon_days) for the forecast line. Uses
    expected_return_pct × likelihood as the projected fraction. Likelihood
    falls back to conviction (legacy) and then to 1.0 if neither is present
    so a conviction without a forecast still draws a line."""
    er = entry.get("expected_return_pct")
    horizon = entry.get("time_to_target_days")
    if er is None or horizon is None or horizon <= 0:
        return None
    if not math.isfinite(today_price) or today_price <= 0:
        return None
    weight = entry.get("likelihood")
    if weight is None:
        weight = entry.get("conviction") or 1.0
    raw = (float(er) / 100.0) * float(weight)
    if not math.isfinite(raw):
        return None
    raw = max(min(raw, _FORECAST_RETURN_CAP), -0.99)
    if (entry.get("direction") or "") == "short":
        raw = -abs(raw)
    return today_price * (1.0 + raw), int(horizon)


# ── Tick placement ───────────────────────────────────────────────────────────

def _hourly_ticks(
    bars: list[dict],
    n_past: int,
    today_et: datetime,
    horizon_days: int,
) -> tuple[list[float], list[str]]:
    """One labelled tick per trading hour across past + future. Format mirrors
    reporting/pnl_curve.py — date appears once per day-boundary, hour-only
    on subsequent ticks. If the total tick count exceeds _MAX_TICK_LABELS
    we sub-sample uniformly so labels stay legible.

    Past x-positions come from the actual bar timestamps (integer index into
    bars[]). Future x-positions are at integer steps from ``n_past`` out to
    ``n_past + horizon_days × _TRADING_HOURS_PER_DAY``.
    """
    raw: list[tuple[float, datetime]] = []

    # Past: every kept bar.
    for i, b in enumerate(bars):
        raw.append((float(i), b["t"].astimezone(_NYSE_TZ)))

    # Future: every trading hour from the next slot out to the horizon.
    n_future = int(horizon_days) * _TRADING_HOURS_PER_DAY
    for k in range(1, n_future + 1):
        x = (n_past - 1) + k
        raw.append((float(x), _project_trading_hour(today_et, k)))

    if not raw:
        return [], []

    # Down-sample uniformly if there are too many candidates.
    if len(raw) > _MAX_TICK_LABELS:
        step = max(1, len(raw) // _MAX_TICK_LABELS)
        sampled = raw[::step]
        if sampled[-1] is not raw[-1]:
            sampled.append(raw[-1])
        raw = sampled

    tick_idxs: list[float] = []
    labels: list[str] = []
    last_date = None
    for x, et in raw:
        d = et.strftime("%m-%d")
        hour = et.strftime("%H:%M")
        label = f"{d} {hour}" if d != last_date else hour
        tick_idxs.append(x)
        labels.append(label)
        last_date = d

    return tick_idxs, labels


# ── Per-row renderer ─────────────────────────────────────────────────────────

def _render_row(ax, entry: dict, bars: list[dict],
                fills: Optional[list[dict]] = None,
                ax_rsi=None,
                daily_closes: Optional[list[float]] = None,
                indicator_spec: Optional[dict] = None) -> None:
    """Plot one ticker row.

    `ax`         — top sub-axes for price + SMA + BBANDS shading + dashed
                   forecast line (and BUY/SELL markers).
    `ax_rsi`     — bottom sub-axes for the RSI oscillator (computed from the
                   hourly past bars). May be None when no oscillator is
                   declared in the agent's indicator spec.
    `daily_closes` — long-window daily close array used to compute trend
                   SMAs and Bollinger envelope. May be empty if the daily
                   fetch failed.
    `indicator_spec` — the agent's `indicators:` block (trend list,
                   oscillator name, envelope name, thresholds).
    """
    import bisect
    import matplotlib.ticker as mticker
    import numpy as np
    from reporting._indicators import (
        sma as _sma, rsi as _rsi, bbands as _bbands,
        parse_indicator_name, indicator_summary,
    )

    sym = entry["symbol"]
    n_past = len(bars)
    today_price: Optional[float] = bars[-1]["c"] if bars else (
        float(entry["mark"]) if entry.get("mark") is not None else None
    )

    # ── Indicator overlays driven by the agent's persona spec ───────────
    # SMAs render as horizontal lines at the latest daily value (the SMA
    # only moves slowly relative to a 5-day past window, so a horizontal
    # at the current value tells the user where price sits vs. trend
    # without dragging in 200 days of x-axis history).
    # BBANDS render as a shaded band between current upper/lower.
    spec = indicator_spec or {}
    indicator_summary_text = ""
    if daily_closes and len(daily_closes) > 5:
        daily_arr = np.asarray(daily_closes, dtype=float)
        # Bollinger envelope (drawn first so the band sits behind everything)
        env = (spec.get("envelope") or "").upper()
        if env.startswith("BBAND"):
            _, period = parse_indicator_name(env)
            if period <= 0:
                period = 20
            up_a, mid_a, lo_a = _bbands(daily_arr, period=period, k=2.0)
            if not np.isnan(up_a[-1]):
                up = float(up_a[-1]); lo = float(lo_a[-1])
                ax.axhspan(lo, up, color="#90caf9", alpha=0.16,
                           zorder=0, label=f"BB({period}) ±2σ")
        # Trend SMAs (overlay lines at current values)
        sma_styles = ["#888", "#4a4a4a"]   # lighter = shorter window
        for idx, trend_name in enumerate(spec.get("trend") or []):
            kind, period = parse_indicator_name(trend_name)
            if kind != "SMA" or period <= 0:
                continue
            sma_arr = _sma(daily_arr, period)
            if np.isnan(sma_arr[-1]):
                continue
            sval = float(sma_arr[-1])
            color = sma_styles[idx % len(sma_styles)]
            ax.axhline(sval, color=color, lw=1.0, ls="--", alpha=0.7,
                       zorder=2, label=f"SMA{period}={sval:.2f}")

        # Compose a brief snapshot string the row title can use.
        snap = indicator_summary(daily_arr, spec)
        if snap:
            bits = []
            if "rsi" in snap:
                bits.append(snap["rsi"] + ((" " + snap["rsi_state"][0:3]) if "rsi_state" in snap else ""))
            for k in sorted(k for k in snap if k.startswith("px_vs_sma")):
                bits.append(snap[k])
            if "bb_state" in snap:
                bits.append(snap["bb_state"])
            indicator_summary_text = " | ".join(bits)

    # Past portion: integer index 0..n_past-1.
    if n_past > 0:
        x_past = list(range(n_past))
        y_past = [b["c"] for b in bars]
        ax.plot(x_past, y_past, lw=2.0, color="#1565c0", zorder=3,
                label=f"{sym} past (hourly)")

        # Day separators on the past portion: vertical dotted line wherever the
        # gap between consecutive in-hours bars exceeds 4 trading hours
        # (i.e. an overnight boundary).
        for i in range(1, n_past):
            if (bars[i]["t"] - bars[i - 1]["t"]).total_seconds() > 4 * 3600:
                ax.axvline(i - 0.5, color="#aaa", lw=0.5, ls="--", alpha=0.55, zorder=1)

        # ── RSI sub-panel ──────────────────────────────────────────────
        # Compute on the hourly past closes so the line is dense across
        # the 5-day window. Daily RSI would be too coarse for this scope.
        if ax_rsi is not None:
            osc = (spec.get("oscillator") or "").upper()
            if osc.startswith("RSI") and n_past >= 4:
                _, rperiod = parse_indicator_name(osc)
                if rperiod <= 0:
                    rperiod = 14
                hourly_close = np.asarray(y_past, dtype=float)
                rsi_series = _rsi(hourly_close, period=rperiod)
                ob = float(spec.get("overbought") or 70)
                os_ = float(spec.get("oversold") or 30)
                # Draw threshold zone first so the RSI line sits over it
                ax_rsi.axhspan(0, os_, color="#a5d6a7", alpha=0.18, zorder=0)
                ax_rsi.axhspan(ob, 100, color="#ef9a9a", alpha=0.18, zorder=0)
                ax_rsi.axhline(ob, color="#c62828", lw=0.6, ls=":", zorder=1)
                ax_rsi.axhline(os_, color="#2e7d32", lw=0.6, ls=":", zorder=1)
                ax_rsi.plot(x_past, rsi_series, color="#7b1fa2", lw=1.2, zorder=2)
                ax_rsi.set_ylim(0, 100)
                ax_rsi.set_yticks([os_, 50, ob])
                ax_rsi.set_yticklabels([f"{int(os_)}", "50", f"{int(ob)}"],
                                        fontsize=9, color="#555")
                ax_rsi.tick_params(axis="x", labelsize=9)
                ax_rsi.grid(True, alpha=0.20)
                ax_rsi.set_ylabel(f"RSI{rperiod}", fontsize=9, color="#555",
                                   labelpad=2)

        # BUY/SELL markers from this agent's ledger — placed at the kept-bar
        # x-position closest to each fill's timestamp. Multiple fills in the
        # same hour are aggregated into one marker by symbol+side+hour so we
        # don't pile triangles on top of each other. Top-12 by |notional| keeps
        # the row readable even on heavily-traded names.
        if fills:
            from collections import defaultdict
            ts_epochs = [b["t"].timestamp() for b in bars]
            agg: dict[tuple, dict] = defaultdict(
                lambda: {"notional": 0.0, "ts": None, "event": None}
            )
            for f in fills:
                ft = f["ts"]
                key = (f["event"], ft.replace(minute=0, second=0, microsecond=0))
                slot = agg[key]
                slot["notional"] += f["agent_qty"] * f["price"]
                if slot["ts"] is None or ft > slot["ts"]:
                    slot["ts"] = ft
                slot["event"] = f["event"]
            groups = sorted(agg.values(), key=lambda s: -abs(s["notional"]))[:12]
            text_objs = []
            for slot in sorted(groups, key=lambda s: s["ts"]):
                e = slot["ts"].timestamp()
                idx = bisect.bisect_left(ts_epochs, e)
                if idx >= n_past:
                    idx = n_past - 1
                elif idx > 0 and (e - ts_epochs[idx - 1]) < (ts_epochs[idx] - e):
                    idx = idx - 1
                y = bars[idx]["c"]
                is_buy = slot["event"] == "LEND"
                marker = "^" if is_buy else "v"
                color = "#2e7d32" if is_buy else "#c62828"
                ax.plot(idx, y, marker=marker, markersize=9, color=color,
                        markeredgecolor="black", markeredgewidth=0.5, zorder=6)
                label = f"{'BUY' if is_buy else 'SELL'} ${abs(slot['notional']):,.0f}"
                seed_dy_pts = 12 if is_buy else -12
                ymin, ymax = ax.get_ylim()
                seed_dy_data = seed_dy_pts * (ymax - ymin) / 200.0
                txt = ax.text(
                    idx, y + seed_dy_data, label,
                    fontsize=9, color=color,
                    ha="center", va="bottom" if is_buy else "top", zorder=7,
                )
                text_objs.append(txt)
            if text_objs:
                try:
                    from adjustText import adjust_text
                    adjust_text(
                        text_objs, ax=ax,
                        only_move={"text": "xy", "static": "xy", "explode": "xy"},
                        expand=(1.2, 1.4),
                        arrowprops=dict(arrowstyle="-", color="#888", alpha=0.45, lw=0.5),
                        force_text=(0.4, 0.9),
                    )
                except ImportError:
                    pass

    # Forecast portion: draw one dashed line per horizon bucket.
    # Horizons are ordered intraday → near → far → cycle; each gets a
    # progressively thinner + more transparent line so the near-term view
    # dominates visually.
    _HORIZON_STYLES: dict[str, dict] = {
        "intraday": {"lw": 2.0, "alpha": 1.00, "ls": "--"},
        "near":     {"lw": 1.6, "alpha": 0.80, "ls": "--"},
        "far":      {"lw": 1.2, "alpha": 0.55, "ls": (0, (5, 5))},
        "cycle":    {"lw": 0.9, "alpha": 0.35, "ls": (0, (3, 7))},
    }
    _HORIZON_ORDER = ("intraday", "near", "far", "cycle")

    all_horizon_rows = entry.get("forecast_horizons") or []
    # Fall back to the primary entry if no horizon rows available
    if not all_horizon_rows:
        primary_forecast = _forecast_endpoint(today_price or 0.0, entry)
        if primary_forecast is not None:
            all_horizon_rows = [entry]

    # Sort by horizon order so intraday is drawn last (on top)
    all_horizon_rows = sorted(
        all_horizon_rows,
        key=lambda h: _HORIZON_ORDER.index(
            (h.get("horizon") or (
                "intraday" if (h.get("time_to_target_days") or 999) <= 1 else
                "near"     if (h.get("time_to_target_days") or 999) <= 5 else
                "far"      if (h.get("time_to_target_days") or 999) <= 30 else
                "cycle"
            ))
        ),
    )

    max_horizon_days = 0
    today_boundary_drawn = False
    if today_price is not None and n_past > 0:
        for h_row in all_horizon_rows:
            forecast = _forecast_endpoint(today_price, h_row)
            if not forecast:
                continue
            end_price, horizon_days = forecast
            x_end = (n_past - 1) + horizon_days * _TRADING_HOURS_PER_DAY
            max_horizon_days = max(max_horizon_days, horizon_days)
            h_name = h_row.get("horizon") or (
                "intraday" if horizon_days <= 1 else
                "near"     if horizon_days <= 5  else
                "far"      if horizon_days <= 30 else
                "cycle"
            )
            style = _HORIZON_STYLES.get(h_name, _HORIZON_STYLES["far"])
            base_color = "#2e7d32" if end_price >= today_price else "#c62828"
            ax.plot([n_past - 1, x_end], [today_price, end_price],
                    lw=style["lw"], ls=style["ls"],
                    color=base_color, alpha=style["alpha"], zorder=3,
                    label=f"{h_name} → {end_price:.2f} ({horizon_days}d)")

            # Endpoint label for the longest horizon only (avoid clutter).
            if h_name in ("cycle", "far") or len(all_horizon_rows) == 1:
                ax.annotate(
                    f"{end_price:.2f}\n{h_name} {horizon_days}d",
                    xy=(x_end, end_price),
                    xytext=(-6, 0), textcoords="offset points",
                    ha="right", va="center",
                    fontsize=10, color=base_color, alpha=max(style["alpha"], 0.7),
                    bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                              edgecolor=base_color, alpha=0.88, linewidth=0.8),
                    zorder=5,
                )

            if not today_boundary_drawn:
                # "Today" boundary — heavier line drawn once.
                ax.axvline(n_past - 1, color="#444", lw=0.9, ls=":", alpha=0.8, zorder=2)
                ax.text(n_past - 1, ax.get_ylim()[1], "  today",
                        fontsize=10, color="#444", va="top", ha="left", zorder=4)
                today_boundary_drawn = True

    # X-axis ticks: span the widest horizon drawn, or history-only if no forecasts.
    if max_horizon_days > 0 and n_past > 0:
        today_et = bars[-1]["t"].astimezone(_NYSE_TZ)
        tick_idxs, labels = _hourly_ticks(bars, n_past, today_et, max_horizon_days)
        x_end_max = (n_past - 1) + max_horizon_days * _TRADING_HOURS_PER_DAY
        if not tick_idxs or abs(tick_idxs[-1] - x_end_max) > 0.5:
            end_dt = _project_trading_hour(today_et, max_horizon_days * _TRADING_HOURS_PER_DAY)
            tick_idxs.append(x_end_max)
            labels.append(end_dt.strftime("%m-%d %H:%M"))
        ax.xaxis.set_major_locator(mticker.FixedLocator(tick_idxs))
        idx_to_label = dict(zip([round(x, 4) for x in tick_idxs], labels))
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(
            lambda val, pos: idx_to_label.get(round(val, 4), "")
        ))
        ax.grid(True, axis="both", alpha=0.25)
        ax.set_xlim(-0.5, x_end_max + max(2.0, x_end_max * 0.04))
    elif n_past > 0:
        # No forecast — past only. Same hourly-labelled ticks as forecast rows.
        today_et = bars[-1]["t"].astimezone(_NYSE_TZ)
        tick_idxs, labels = _hourly_ticks(bars, n_past, today_et, horizon_days=0)
        ax.xaxis.set_major_locator(mticker.FixedLocator(tick_idxs))
        idx_to_label = dict(zip([round(x, 4) for x in tick_idxs], labels))
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(
            lambda val, pos: idx_to_label.get(round(val, 4), "")
        ))
        ax.grid(True, axis="both", alpha=0.25)
        ax.set_xlim(-0.5, n_past - 0.5 + 0.5)

        # Boxed last-price annotation (mirrors the polished P&L curve).
        ax.annotate(
            f"{today_price:.2f}" if today_price is not None else "",
            xy=(n_past - 1, today_price or 0.0),
            xytext=(8, 0), textcoords="offset points",
            ha="left", va="center", fontsize=11,
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                      edgecolor="#1565c0", alpha=0.92, linewidth=0.9),
            zorder=5,
        )
    else:
        ax.text(0.5, 0.5, f"{sym}: bars unavailable",
                transform=ax.transAxes, ha="center", va="center",
                fontsize=13, color="#888")
        ax.set_xticks([])
        ax.set_yticks([])

    # Two-line title so each row's metadata is fully visible without
    # truncation. Line 1 = forecast inputs (SYM | direction conv | E[ret] |
    # horizon | score | flags). Line 2 = method + position + indicator-
    # state snapshot.
    line1: list[str] = [sym]
    conv = entry.get("conviction") or 0.0
    if conv:
        direction = entry.get("direction") or "?"
        line1.append(f"{direction} conv={conv:.2f}")
    er = entry.get("expected_return_pct")
    lk = entry.get("likelihood")
    if er is not None:
        if lk is not None:
            line1.append(f"E[ret]={er:+.1f}%×L={lk:.2f}")
        else:
            line1.append(f"E[ret]={er:+.1f}%")
    ttd = entry.get("time_to_target_days")
    if ttd is not None:
        line1.append(f"horizon={ttd}d")
    score = entry.get("forecast_score")
    if score is not None:
        line1.append(f"score={float(score):+.3f}")
    if entry.get("momentum_confirmed") is True:
        line1.append("✓momentum")
    elif entry.get("momentum_confirmed") is False:
        line1.append("⚠early")
    if entry.get("_universe_only"):
        line1.append("watching")

    line2: list[str] = []
    method = entry.get("method")
    if method:
        m = str(method).strip()
        line2.append(f"via \"{m[:50]}\"" if len(m) <= 50 else f"via \"{m[:47]}…\"")
    mv = entry.get("market_value") or 0.0
    if mv:
        line2.append(f"pos=${mv:,.0f}")
    if indicator_summary_text:
        line2.append(indicator_summary_text)

    # Escape `$` so matplotlib's mathtext parser doesn't try to render
    # something like "$137.75" as math mode and raise ParseException.
    title_text = (" | ".join(line1)
                   + ("\n" + " | ".join(line2) if line2 else "")
                  ).replace("$", r"\$")
    ax.set_title(title_text, fontsize=13, loc="left")

    # NOTE: per-axis major/minor grid is configured inside the branches above
    # so we don't reset minor gridlines here.
    ax.tick_params(axis="x", labelsize=10)
    ax.tick_params(axis="y", labelsize=10)
    plt = sys.modules.get("matplotlib.pyplot")
    if plt is not None:
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right",
                 rotation_mode="anchor")


# ── Top-level renderer ───────────────────────────────────────────────────────

async def render_forecast_panel(agent_name: str) -> Optional[Path]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as _plt
    from matplotlib.gridspec import GridSpec

    rows = await _select_top_tickers(agent_name)
    if not rows:
        log.info("forecast_panel: agent=%s has no convictions or positions", agent_name)
        return None

    indicator_spec = _load_agent_indicators(agent_name)

    sem = asyncio.Semaphore(4)

    async def _fetch_hourly(sym: str) -> list[dict]:
        async with sem:
            return await _fetch_hourly_closes(sym)

    async def _fetch_daily(sym: str) -> list[float]:
        async with sem:
            return await _fetch_daily_closes(sym, n_days=300)

    bar_results, daily_results = await asyncio.gather(
        asyncio.gather(*[_fetch_hourly(r["symbol"]) for r in rows]),
        asyncio.gather(*[_fetch_daily(r["symbol"]) for r in rows]),
    )

    # Past-window fills for BUY/SELL markers — one query per agent for all
    # symbols in the panel.
    fills_since = datetime.now(timezone.utc) - timedelta(days=_PAST_TRADING_DAYS + 4)
    fills_by_symbol = await _fetch_recent_fills(
        agent_name, {r["symbol"] for r in rows}, fills_since,
    )

    n = len(rows)
    # 2-column × ceil(n/2)-row grid. Aspect tuned so the panel matches
    # the slide's chart-half cell aspect (≈ 0.80, slightly portrait):
    # cell_w/cell_h × n_cols/n_rows = 8/4 × 2/5 = 0.80.
    n_cols = 2 if n > 1 else 1
    n_rows = (n + n_cols - 1) // n_cols
    cell_w = 8.0
    cell_h = 4.0
    fig_width = cell_w * n_cols
    fig_height = max(cell_h * n_rows, 4.5)
    fig = _plt.figure(figsize=(fig_width, fig_height), facecolor="white")
    gs_outer = GridSpec(
        nrows=n_rows, ncols=n_cols,
        hspace=0.55, wspace=0.14, figure=fig,
    )
    today = datetime.now(timezone.utc)

    n_with_forecast = 0
    for i, (entry, bars, daily) in enumerate(zip(rows, bar_results, daily_results)):
        # Row-major fill: top-left, top-right, then second row, etc.
        gr = i // n_cols
        gc = i % n_cols
        sub = gs_outer[gr, gc].subgridspec(
            nrows=2, ncols=1, height_ratios=[3.5, 1.0], hspace=0.05,
        )
        ax_top = fig.add_subplot(sub[0])
        ax_rsi = fig.add_subplot(sub[1], sharex=ax_top)
        sym_fills = fills_by_symbol.get(entry["symbol"], [])
        _render_row(
            ax_top, entry, bars, sym_fills,
            ax_rsi=ax_rsi, daily_closes=daily, indicator_spec=indicator_spec,
        )
        # Hide the top subplot's tick labels since the RSI sub-panel carries
        # the date axis (sharex makes them automatic; clean up labels).
        _plt.setp(ax_top.get_xticklabels(), visible=False)
        ax_top.tick_params(axis="x", which="both", length=0)
        if entry.get("expected_return_pct") is not None and entry.get("time_to_target_days"):
            n_with_forecast += 1

    notes = (indicator_spec.get("notes") or "").strip().split("\n")[0]
    fig.suptitle(
        f"{agent_name.upper()} — top {n} forecast panel — "
        f"{today.astimezone(_NYSE_TZ).strftime('%Y-%m-%d %H:%M ET')}\n"
        f"hourly past + dashed forecast · trend={','.join(indicator_spec.get('trend') or [])} "
        f"· osc={indicator_spec.get('oscillator')} · env={indicator_spec.get('envelope')} · "
        f"{n_with_forecast}/{n} rows with forecast line"
        + (f"\n{notes}" if notes else ""),
        fontsize=14, y=0.998,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.984))

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = _OUT_DIR / f"forecast_{agent_name}_{today.strftime('%Y%m%d_%H%M%S')}.png"
    fig.savefig(out, dpi=200)
    _plt.close(fig)
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────

def _main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--agent", required=True)
    args = p.parse_args()

    async def run() -> None:
        path = await render_forecast_panel(args.agent)
        if path:
            print(str(path))
        else:
            print("(no convictions or positions for agent)", file=sys.stderr)
            sys.exit(2)

    asyncio.run(run())


if __name__ == "__main__":
    _main()
