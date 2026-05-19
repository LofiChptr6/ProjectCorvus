#!/usr/bin/env python3
"""Daily OHLCV ingestor — Massive.com → local_bars_daily.

Runs once per trading day after RTH close (systemd: trading-daily-bars.timer
at 17:30 ET Mon–Fri). For every symbol in `store.list_streamer_symbols()`
(watchlist ∪ current positions) it fetches the trailing 365 days of daily
bars from Massive and UPSERTs into `local_bars_daily`. Idempotent across
runs — the same day's row will be re-stamped on restatement.

Why separate from `stream_bars.py`:
  - Different cadence (1×/day vs every 5 min during RTH).
  - Different table / retention (1Y daily vs 14d 5-min).
  - The 5-min streamer is intraday-pacing-critical; the daily fetch is heavy
    but happens off-hours, so concurrency / pacing concerns differ.

Manual:
    python scripts/ingest_daily_bars.py
    python scripts/ingest_daily_bars.py --symbols AAPL,SPY
    python scripts/ingest_daily_bars.py --days 365     # backfill window
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date, datetime, timezone
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

from data import massive_client
from db import store

log = logging.getLogger("ingest_daily_bars")

CONCURRENT_FETCHES = 8


async def _fetch_one(symbol: str, days: int, sem: asyncio.Semaphore) -> list[dict]:
    async with sem:
        try:
            data = await massive_client.get_bars(
                symbol, bar_size="1 day", duration=f"{days} D",
            )
        except Exception as e:
            log.warning("get_bars %s: %s: %s", symbol, type(e).__name__, e)
            return []
    bars = data.get("bars") or []
    out: list[dict] = []
    for b in bars:
        ts = b.get("t")
        if not ts:
            continue
        try:
            bar_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if bar_dt.tzinfo is None:
                bar_dt = bar_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if b.get("o") is None or b.get("c") is None:
            continue
        out.append({
            "symbol": symbol,
            "bar_date": bar_dt.date(),
            "open": b["o"],
            "high": b.get("h") or b["o"],
            "low": b.get("l") or b["o"],
            "close": b["c"],
            "volume": b.get("v") or 0.0,
        })
    return out


async def _run(symbols: list[str], days: int) -> dict:
    started = datetime.now(timezone.utc)
    if not symbols:
        symbols = await store.list_streamer_symbols()
    if not symbols:
        log.info("no symbols to ingest (watchlist + positions both empty)")
        return {"symbols": 0, "rows": 0, "duration_ms": 0}

    log.info("daily ingest start: %d symbols × %d days lookback", len(symbols), days)
    sem = asyncio.Semaphore(CONCURRENT_FETCHES)
    results = await asyncio.gather(*(_fetch_one(s, days, sem) for s in symbols))
    all_rows = [r for bars in results for r in bars]
    n_upserted = await store.upsert_local_bars_daily(all_rows)
    dur_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    log.info("daily ingest done: %d symbols, %d rows upserted, %dms",
             len(symbols), n_upserted, dur_ms)
    return {"symbols": len(symbols), "rows": n_upserted, "duration_ms": dur_ms}


def cli() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--symbols", help="Comma-separated tickers; default = watchlist ∪ positions")
    p.add_argument("--days", type=int, default=365, help="Lookback window in days (default 365)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not os.environ.get("MASSIVE_API_KEY", "").strip():
        log.error("MASSIVE_API_KEY missing — cannot fetch bars")
        return 2
    syms = [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()]
    try:
        asyncio.run(_run(syms, args.days))
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(cli())
