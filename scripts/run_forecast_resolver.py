#!/usr/bin/env python3
"""Forecast outcome resolver — fills in agent_forecast.realized_return_pct
for forecasts whose horizon has elapsed.

Without this, agent_forecast collects predictions forever without ever
measuring whether they were right. There is no accuracy/calibration loop
without it. Each hour the resolver scans for forecasts where
submitted_at + time_to_target_days * 1.4 calendar days has passed AND
resolved_at IS NULL, fetches daily bars from Massive, and computes the
realized close→close return.

Resolution sources (recorded in agent_forecast.resolution_source):
  bars          — real bars found, realized_return_pct populated
  missing_bars  — Massive returned no usable bars for this symbol
  stale         — bars cover today but not the original submitted_at window

UPSERT in store.upsert_forecast_batch already resets these three columns
to NULL when an agent re-publishes the same (agent_name, symbol, horizon),
so a fresh prediction always starts a fresh outcome window.

Exit codes:
    0  ran successfully (any number of resolutions, including zero)
    1  unexpected runtime error
    2  skipped (no env / no DB config detected — should not happen on the box)
"""
from __future__ import annotations

import asyncio
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
    """First bar whose calendar date is >= on_or_after. Bars are sorted ASC."""
    for b in bars:
        d = _bar_date(b)
        if d is not None and d >= on_or_after:
            return b
    return None


def _find_exit_bar(bars: list[dict], on_or_before: date) -> dict | None:
    """Last bar whose calendar date is <= on_or_before. Bars are sorted ASC,
    so walk from the end."""
    for b in reversed(bars):
        d = _bar_date(b)
        if d is not None and d <= on_or_before:
            return b
    return None


async def _fetch_bars_for(symbol: str) -> list[dict]:
    """Daily bars for a single symbol covering the resolver's working window
    (the longest cycle horizon is ~30 trading days = ~42 calendar days, so
    90 days is comfortable)."""
    from data.massive_client import get_bars
    try:
        resp = await get_bars(symbol=symbol, bar_size="1 day", duration="90 D")
    except Exception as exc:
        log.warning("get_bars failed for %s: %s", symbol, exc)
        return []
    return resp.get("bars") or []


async def _resolve_symbol(conn, symbol: str, rows: list[dict]) -> dict:
    """Resolve every forecast on `symbol` in `rows`. Returns counts."""
    counts = {"bars": 0, "missing_bars": 0, "stale": 0}
    bars = await _fetch_bars_for(symbol)

    for r in rows:
        forecast_id = r["id"]
        submitted_at: datetime = r["submitted_at"]
        ttd: int = r["time_to_target_days"]
        target_date = (submitted_at.date()
                       + timedelta(days=int(round(ttd * _TRADING_TO_CALENDAR))))
        if not bars:
            await conn.execute(
                "UPDATE agent_forecast SET realized_return_pct=NULL, "
                "resolved_at=NOW(), resolution_source='missing_bars' WHERE id=$1",
                forecast_id,
            )
            counts["missing_bars"] += 1
            log.info("resolved id=%s sym=%s source=missing_bars", forecast_id, symbol)
            continue

        entry = _find_entry_bar(bars, submitted_at.date())
        exit_ = _find_exit_bar(bars, target_date)

        if entry is None or exit_ is None:
            # Bars exist but don't span the [submitted_at, target_date] window.
            # Most likely cause: forecast was submitted before Massive's 90-day
            # window opened — we can't recover the entry close.
            await conn.execute(
                "UPDATE agent_forecast SET realized_return_pct=NULL, "
                "resolved_at=NOW(), resolution_source='stale' WHERE id=$1",
                forecast_id,
            )
            counts["stale"] += 1
            log.info("resolved id=%s sym=%s source=stale entry=%s exit=%s",
                     forecast_id, symbol,
                     _bar_date(entry) if entry else None,
                     _bar_date(exit_) if exit_ else None)
            continue

        entry_c = float(entry["c"])
        exit_c = float(exit_["c"])
        if entry_c <= 0:
            await conn.execute(
                "UPDATE agent_forecast SET realized_return_pct=NULL, "
                "resolved_at=NOW(), resolution_source='missing_bars' WHERE id=$1",
                forecast_id,
            )
            counts["missing_bars"] += 1
            continue
        realized = (exit_c - entry_c) / entry_c * 100.0
        await conn.execute(
            "UPDATE agent_forecast SET realized_return_pct=$1, "
            "resolved_at=NOW(), resolution_source='bars' WHERE id=$2",
            realized, forecast_id,
        )
        counts["bars"] += 1
        expected = float(r["expected_return_pct"])
        log.info(
            "resolved id=%s sym=%s agent=%s horizon=%s "
            "entry=%.4f@%s exit=%.4f@%s realized=%+.2f%% expected=%+.2f%%",
            forecast_id, symbol, r["agent_name"], r["horizon"],
            entry_c, _bar_date(entry), exit_c, _bar_date(exit_), realized, expected,
        )
    return counts


async def main() -> int:
    start = time.time()
    log.info("resolver starting")

    from db.schema import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, agent_name, symbol, horizon, expected_return_pct,
                   time_to_target_days, submitted_at
            FROM agent_forecast
            WHERE resolved_at IS NULL
              AND submitted_at + (time_to_target_days * $1 * INTERVAL '1 day') < NOW()
            ORDER BY submitted_at
            LIMIT 1000
            """,
            _TRADING_TO_CALENDAR,
        )
        if not rows:
            log.info("nothing to resolve (no forecasts past horizon)")
            return 0

        # Bucket by symbol — one Massive fetch per distinct symbol.
        by_symbol: dict[str, list[dict]] = {}
        for r in rows:
            by_symbol.setdefault(r["symbol"].upper(), []).append(dict(r))
        log.info("queue: %d forecasts across %d symbols", len(rows), len(by_symbol))

        totals = {"bars": 0, "missing_bars": 0, "stale": 0}
        for symbol, rs in sorted(by_symbol.items()):
            try:
                counts = await _resolve_symbol(conn, symbol, rs)
                for k, v in counts.items():
                    totals[k] += v
            except Exception as exc:
                log.exception("symbol %s resolution failed: %s", symbol, exc)

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
