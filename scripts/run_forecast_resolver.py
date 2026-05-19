#!/usr/bin/env python3
"""Forecast outcome resolver — fills in agent_forecast.realized_return_pct
and the calibration scores (log-loss, Brier, CRPS, pinball05/95, realized_bin_idx)
for forecasts whose horizon has elapsed.

Without this, agent_forecast collects predictions forever without ever
measuring whether they were right. There is no accuracy/calibration loop
without it.

Two resolution paths:
  - intraday (horizon ∈ {5m, 1h}): reads from `local_bars` table directly
    (5-min bars cached, 14d retention; sub-min granularity not stored).
    Resolution boundary is the first bar with bar_time ≥ submitted_at + horizon.
  - daily (horizon ∈ {intraday, near, far, cycle, 1d, 1w}): fetches daily bars
    from Massive and matches by calendar date with the ~1.4 trading-to-calendar
    multiplier. (1d/1w distributions live in 'near'/'far' rows for the legacy
    column; the distribution carries its own canonical horizon string.)

Resolution sources (agent_forecast.resolution_source):
  bars          — real bars found, realized_return_pct populated
  missing_bars  — Massive/local_bars returned no usable data for this symbol
  stale         — bars exist but don't span the submitted_at→target window

Calibration scoring runs immediately after realized_return_pct is computed,
on any row carrying a `distribution` payload. Legacy scalar-only rows get
only realized_return_pct (today's behavior).

UPSERT in store.upsert_forecasts_batch already resets these columns to NULL
on republish, so a fresh prediction starts a fresh outcome window.

Exit codes:
    0  ran successfully (any number of resolutions, including zero)
    1  unexpected runtime error
    2  skipped (no env / no DB config detected — should not happen on the box)
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import find_dotenv, load_dotenv
    found = find_dotenv(usecwd=True)
    if found:
        load_dotenv(found)
except Exception:
    pass


def _setup_logging() -> logging.Logger:
    log_path = _REPO_ROOT / "logs" / "forecast-resolver.log"
    log_path.parent.mkdir(exist_ok=True)
    logger = logging.getLogger("forecast_resolver")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(log_path)
        fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s",
                                          datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(fh)
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(sh)
    return logger


log = _setup_logging()


# 1 trading day ≈ 1.4 calendar days (weekends + occasional holidays). Pad
# generously — we'd rather wait an extra day than mis-resolve mid-horizon.
_TRADING_TO_CALENDAR = 1.4
_INTRADAY_HORIZONS = {"5m", "1h"}
_INTRADAY_HORIZON_MINUTES = {"5m": 5, "1h": 60}


def _bar_date(bar: dict) -> date | None:
    """Pull the calendar date out of a bar's ISO timestamp."""
    t = bar.get("t")
    if not t:
        return None
    try:
        return datetime.fromisoformat(t).date()
    except ValueError:
        return None


def _find_entry_bar(bars: list[dict], on_or_after: date) -> dict | None:
    for b in bars:
        d = _bar_date(b)
        if d is not None and d >= on_or_after:
            return b
    return None


def _find_exit_bar(bars: list[dict], on_or_before: date) -> dict | None:
    for b in reversed(bars):
        d = _bar_date(b)
        if d is not None and d <= on_or_before:
            return b
    return None


async def _fetch_daily_bars(symbol: str) -> list[dict]:
    """Daily bars covering the resolver's working window (90 days)."""
    from data.massive_client import get_bars
    try:
        resp = await get_bars(symbol=symbol, bar_size="1 day", duration="90 D")
    except Exception as exc:
        log.warning("get_bars(daily) failed for %s: %s", symbol, exc)
        return []
    return resp.get("bars") or []


async def _fetch_intraday_bars(conn, symbol: str, since: datetime) -> list[dict]:
    """5-min bars from local_bars covering [since, now]. Returns list of dicts
    with bar_time + close, sorted ascending."""
    rows = await conn.fetch(
        """SELECT bar_time, close FROM local_bars
           WHERE symbol = $1 AND interval = '5min' AND bar_time >= $2
           ORDER BY bar_time ASC""",
        symbol.upper(), since,
    )
    return [{"bar_time": r["bar_time"], "c": float(r["close"])} for r in rows]


def _intraday_pair(bars: list[dict], submitted_at: datetime,
                   horizon_minutes: int) -> tuple[float, float] | None:
    """Find (entry_close, exit_close) where entry = first bar at/after
    submitted_at and exit = first bar at/after submitted_at + horizon_minutes."""
    if not bars:
        return None
    target = submitted_at + timedelta(minutes=horizon_minutes)
    entry = None
    exit_ = None
    for b in bars:
        ts = b["bar_time"]
        if entry is None and ts >= submitted_at:
            entry = b
        if exit_ is None and ts >= target:
            exit_ = b
            break
    if entry is None or exit_ is None:
        return None
    return float(entry["c"]), float(exit_["c"])


def _apply_scoring(distribution_raw, realized_pct: float) -> dict | None:
    """Parse a distribution payload (may be JSON str or dict) and return the
    score dict, or None on parse/empty failure."""
    if distribution_raw is None:
        return None
    if isinstance(distribution_raw, str):
        try:
            distribution = json.loads(distribution_raw)
        except json.JSONDecodeError:
            return None
    else:
        distribution = distribution_raw
    if not distribution or not distribution.get("bins"):
        return None
    from meta_agent.calibration_scores import score_distribution
    return score_distribution(distribution, realized_pct)


async def _persist_resolution(
    conn, forecast_id: int, *, source: str,
    realized_pct: float | None, scores: dict | None,
) -> None:
    """Write the resolution + (optional) scores to agent_forecast."""
    if scores:
        await conn.execute(
            """UPDATE agent_forecast SET
                 realized_return_pct = $1,
                 resolved_at         = NOW(),
                 resolution_source   = $2,
                 realized_bin_idx    = $3,
                 score_logloss       = $4,
                 score_brier         = $5,
                 score_crps          = $6,
                 score_pinball05     = $7,
                 score_pinball95     = $8
               WHERE id = $9""",
            realized_pct, source,
            int(scores["realized_bin_idx"]),
            float(scores["log_loss"]), float(scores["brier"]),
            float(scores["crps"]),
            float(scores["pinball05"]), float(scores["pinball95"]),
            forecast_id,
        )
    else:
        await conn.execute(
            """UPDATE agent_forecast SET
                 realized_return_pct = $1,
                 resolved_at         = NOW(),
                 resolution_source   = $2
               WHERE id = $3""",
            realized_pct, source, forecast_id,
        )


async def _resolve_intraday(conn, symbol: str, rows: list[dict]) -> dict:
    """Resolve rows with horizon in {5m, 1h} via local_bars (5-min) reads."""
    counts = {"bars": 0, "missing_bars": 0, "stale": 0}
    if not rows:
        return counts
    earliest = min(r["submitted_at"] for r in rows)
    bars = await _fetch_intraday_bars(conn, symbol, earliest - timedelta(minutes=10))

    for r in rows:
        forecast_id = r["id"]
        submitted_at: datetime = r["submitted_at"]
        horizon = r["horizon"]
        h_min = _INTRADAY_HORIZON_MINUTES[horizon]

        if not bars:
            await _persist_resolution(conn, forecast_id, source="missing_bars",
                                      realized_pct=None, scores=None)
            counts["missing_bars"] += 1
            log.info("resolved id=%s sym=%s horizon=%s source=missing_bars",
                     forecast_id, symbol, horizon)
            continue

        pair = _intraday_pair(bars, submitted_at, h_min)
        if pair is None:
            await _persist_resolution(conn, forecast_id, source="stale",
                                      realized_pct=None, scores=None)
            counts["stale"] += 1
            log.info("resolved id=%s sym=%s horizon=%s source=stale",
                     forecast_id, symbol, horizon)
            continue

        entry_c, exit_c = pair
        if entry_c <= 0:
            await _persist_resolution(conn, forecast_id, source="missing_bars",
                                      realized_pct=None, scores=None)
            counts["missing_bars"] += 1
            continue
        realized = (exit_c - entry_c) / entry_c * 100.0
        scores = _apply_scoring(r.get("distribution"), realized)
        await _persist_resolution(conn, forecast_id, source="bars",
                                  realized_pct=realized, scores=scores)
        counts["bars"] += 1
        log.info(
            "resolved id=%s sym=%s agent=%s horizon=%s entry=%.4f exit=%.4f "
            "realized=%+.4f%% scored=%s",
            forecast_id, symbol, r["agent_name"], horizon,
            entry_c, exit_c, realized, scores is not None,
        )
    return counts


async def _resolve_daily(conn, symbol: str, rows: list[dict]) -> dict:
    """Resolve rows whose horizon resolves on a daily-bar window."""
    counts = {"bars": 0, "missing_bars": 0, "stale": 0}
    if not rows:
        return counts
    bars = await _fetch_daily_bars(symbol)

    for r in rows:
        forecast_id = r["id"]
        submitted_at: datetime = r["submitted_at"]
        ttd: int = r["time_to_target_days"]
        target_date = (submitted_at.date()
                       + timedelta(days=int(round(ttd * _TRADING_TO_CALENDAR))))

        if not bars:
            await _persist_resolution(conn, forecast_id, source="missing_bars",
                                      realized_pct=None, scores=None)
            counts["missing_bars"] += 1
            log.info("resolved id=%s sym=%s source=missing_bars", forecast_id, symbol)
            continue

        entry = _find_entry_bar(bars, submitted_at.date())
        exit_ = _find_exit_bar(bars, target_date)
        if entry is None or exit_ is None:
            await _persist_resolution(conn, forecast_id, source="stale",
                                      realized_pct=None, scores=None)
            counts["stale"] += 1
            log.info("resolved id=%s sym=%s source=stale entry=%s exit=%s",
                     forecast_id, symbol,
                     _bar_date(entry) if entry else None,
                     _bar_date(exit_) if exit_ else None)
            continue

        entry_c = float(entry["c"])
        exit_c = float(exit_["c"])
        if entry_c <= 0:
            await _persist_resolution(conn, forecast_id, source="missing_bars",
                                      realized_pct=None, scores=None)
            counts["missing_bars"] += 1
            continue
        realized = (exit_c - entry_c) / entry_c * 100.0
        scores = _apply_scoring(r.get("distribution"), realized)
        await _persist_resolution(conn, forecast_id, source="bars",
                                  realized_pct=realized, scores=scores)
        counts["bars"] += 1
        expected = float(r["expected_return_pct"])
        log.info(
            "resolved id=%s sym=%s agent=%s horizon=%s "
            "entry=%.4f@%s exit=%.4f@%s realized=%+.2f%% expected=%+.2f%% scored=%s",
            forecast_id, symbol, r["agent_name"], r["horizon"],
            entry_c, _bar_date(entry), exit_c, _bar_date(exit_),
            realized, expected, scores is not None,
        )
    return counts


async def main() -> int:
    start = time.time()
    log.info("resolver starting")

    from db.schema import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Two queries — intraday horizons elapse in minutes; daily horizons
        # elapse in days. Issuing both as ORDER BY submitted_at + LIMIT keeps
        # a single worker bounded under spikes.
        intraday_rows = await conn.fetch(
            """
            SELECT id, agent_name, symbol, horizon, expected_return_pct,
                   time_to_target_days, submitted_at, distribution
            FROM agent_forecast
            WHERE resolved_at IS NULL
              AND horizon = ANY($1::text[])
              AND submitted_at + (
                  CASE horizon WHEN '5m' THEN INTERVAL '5 minutes'
                               WHEN '1h' THEN INTERVAL '1 hour'
                  END
              ) < NOW()
            ORDER BY submitted_at
            LIMIT 1000
            """,
            sorted(_INTRADAY_HORIZONS),
        )
        daily_rows = await conn.fetch(
            """
            SELECT id, agent_name, symbol, horizon, expected_return_pct,
                   time_to_target_days, submitted_at, distribution
            FROM agent_forecast
            WHERE resolved_at IS NULL
              AND horizon <> ALL($1::text[])
              AND submitted_at + (time_to_target_days * $2 * INTERVAL '1 day') < NOW()
            ORDER BY submitted_at
            LIMIT 1000
            """,
            sorted(_INTRADAY_HORIZONS),
            _TRADING_TO_CALENDAR,
        )

        if not intraday_rows and not daily_rows:
            log.info("nothing to resolve")
            return 0

        # Bucket each by symbol
        intraday_by_sym: dict[str, list[dict]] = {}
        for r in intraday_rows:
            intraday_by_sym.setdefault(r["symbol"].upper(), []).append(dict(r))
        daily_by_sym: dict[str, list[dict]] = {}
        for r in daily_rows:
            daily_by_sym.setdefault(r["symbol"].upper(), []).append(dict(r))
        log.info("queue: intraday=%d daily=%d (intraday symbols=%d daily symbols=%d)",
                 len(intraday_rows), len(daily_rows),
                 len(intraday_by_sym), len(daily_by_sym))

        totals = {"bars": 0, "missing_bars": 0, "stale": 0}
        for symbol, rs in sorted(intraday_by_sym.items()):
            try:
                counts = await _resolve_intraday(conn, symbol, rs)
                for k, v in counts.items():
                    totals[k] += v
            except Exception as exc:
                log.exception("intraday symbol %s resolution failed: %s", symbol, exc)

        for symbol, rs in sorted(daily_by_sym.items()):
            try:
                counts = await _resolve_daily(conn, symbol, rs)
                for k, v in counts.items():
                    totals[k] += v
            except Exception as exc:
                log.exception("daily symbol %s resolution failed: %s", symbol, exc)

    log.info(
        "resolver done in %.1fs — bars=%d missing_bars=%d stale=%d",
        time.time() - start, totals["bars"], totals["missing_bars"], totals["stale"],
    )
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    except Exception:
        log.exception("resolver crashed")
        rc = 1
    sys.exit(rc)
