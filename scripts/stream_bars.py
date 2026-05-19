#!/usr/bin/env python3
"""5-min OHLCV bar streamer — Massive.com → local Postgres.

Long-running daemon. Every 5 wall-clock minutes during RTH it fans out to
Massive for every symbol on a) any active watchlist row or b) the latest
positions_anchor snapshot, then UPSERTs the day's bars into local_bars.

DESK_POLICY: IBKR gateway is order-only. ALL market data — quotes, bars,
news — flows from Massive.com or its local cache (this table). If a future
caller is tempted to reach into ibkr.market_data for bars: don't. The single
client_id lives with mike's allocator; competing connections silently knock
it offline and corrupt order state.

After every successful cycle the streamer also:
  - prunes rows older than 14 days
  - calls the indicator-compute / OCAP-trigger hook (Phase 3 wires this up)

Manual:
    python scripts/stream_bars.py              # long-running, one cycle / 5 min RTH
    python scripts/stream_bars.py --once       # one cycle and exit (test)
    python scripts/stream_bars.py --force      # ignore RTH gate
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

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

log = logging.getLogger("stream_bars")

NY = ZoneInfo("America/New_York")
INTERVAL_LABEL = "5min"
CYCLE_SECONDS = 300         # 5 min
CONCURRENT_FETCHES = 8      # Massive batch knob — be a nice citizen
RETENTION_DAYS = 14


def _is_rth_now(now_ny: datetime | None = None) -> bool:
    """True during the extended US trading window (04:00–20:05 ET Mon–Fri).
    Includes pre-market (04:00–09:30) and after-hours (16:00–20:00) sessions
    — Massive aggregates have ETH bars and pre/post action carries real
    signal (macro overnight news, earnings reactions, futures-driven gaps).
    Weekends still skip entirely — Massive returns nothing new on Sat/Sun.

    Name kept as `_is_rth_now` for backward compat with CLI flags / log
    messages, but the gate is wider than strict RTH."""
    now = now_ny or datetime.now(NY)
    if now.isoweekday() >= 6:
        return False
    # 04:00 = pre-market open. 20:05 = 5-min buffer past the 20:00 close so
    # the last after-hours bar lands before we idle.
    minutes = now.hour * 60 + now.minute
    return 4 * 60 <= minutes <= 20 * 60 + 5


async def _fetch_one(symbol: str, sem: asyncio.Semaphore) -> list[dict]:
    """Pull today's 5-min bars for one symbol via Massive. Returns list of
    bar dicts ready for upsert_local_bars (UTC datetimes, floats)."""
    async with sem:
        try:
            data = await massive_client.get_bars(
                symbol, bar_size="5 mins", duration="1 D",
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
            bar_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if bar_time.tzinfo is None:
                bar_time = bar_time.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if b.get("o") is None or b.get("c") is None:
            continue
        out.append({
            "symbol": symbol,
            "bar_time": bar_time,
            "interval": INTERVAL_LABEL,
            "open": b["o"],
            "high": b.get("h") or b["o"],
            "low": b.get("l") or b["o"],
            "close": b["c"],
            "volume": b.get("v") or 0.0,
        })
    return out


async def _trigger_indicator_hook(latest_bars_by_symbol: dict[str, dict]) -> None:
    """Phase 3 wires indicator compute + OCAP triggers here. The hook receives
    {symbol: latest_bar_dict} for every symbol that produced a new row this
    cycle. Kept as a no-op import-on-call so the streamer ships before Phase 3
    lands and we don't get an import cycle while iterating."""
    try:
        from analysis import indicators_ocap  # noqa: F401
    except ImportError:
        return
    try:
        await indicators_ocap.on_bars_arrived(latest_bars_by_symbol)
    except Exception as exc:
        log.warning("indicator_hook: %s: %s", type(exc).__name__, exc)


async def _one_cycle() -> dict:
    started = datetime.now(timezone.utc)
    symbols = await store.list_streamer_symbols()
    if not symbols:
        log.info("no streamer symbols (watchlist + positions both empty); skipping")
        return {"symbols": 0, "rows": 0, "pruned": 0, "duration_ms": 0}

    sem = asyncio.Semaphore(CONCURRENT_FETCHES)
    results = await asyncio.gather(*(_fetch_one(s, sem) for s in symbols))

    all_rows: list[dict] = []
    latest_by_symbol: dict[str, dict] = {}
    for bars in results:
        if not bars:
            continue
        all_rows.extend(bars)
        latest_by_symbol[bars[-1]["symbol"]] = bars[-1]

    n_upserted = await store.upsert_local_bars(all_rows)
    n_pruned = await store.prune_local_bars(RETENTION_DAYS)
    await _trigger_indicator_hook(latest_by_symbol)

    dur_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    log.info("cycle: %d symbols, %d rows upserted, %d pruned, %dms",
             len(symbols), n_upserted, n_pruned, dur_ms)
    return {
        "symbols": len(symbols),
        "rows": n_upserted,
        "pruned": n_pruned,
        "duration_ms": dur_ms,
    }


def _sleep_until_next_5min() -> float:
    """Seconds to sleep until the next wall-clock multiple of 5 min, +5s buffer
    so we land after Massive has the freshly-closed bar."""
    now = datetime.now(NY)
    minute = now.minute
    next_min = ((minute // 5) + 1) * 5
    if next_min >= 60:
        next_min = 0
        next_hour = now.replace(minute=0, second=5, microsecond=0)
        next_hour = next_hour.replace(hour=(now.hour + 1) % 24)
        delta = next_hour - now
        # Crossing midnight is fine for the math — sleep delta is positive.
        return max(1.0, delta.total_seconds())
    target = now.replace(minute=next_min, second=5, microsecond=0)
    return max(1.0, (target - now).total_seconds())


async def main(once: bool, force: bool) -> int:
    if not os.environ.get("MASSIVE_API_KEY", "").strip():
        log.error("MASSIVE_API_KEY missing — cannot fetch bars")
        return 2

    if once:
        if not force and not _is_rth_now():
            log.info("outside RTH; --once + no --force ⇒ exit 0 without fetching")
            return 0
        await _one_cycle()
        return 0

    log.info("streamer up (cycle=%ds, retention=%dd, concurrent=%d)",
             CYCLE_SECONDS, RETENTION_DAYS, CONCURRENT_FETCHES)
    while True:
        if force or _is_rth_now():
            try:
                await _one_cycle()
            except Exception as exc:
                log.exception("cycle failed: %s: %s", type(exc).__name__, exc)
        else:
            log.debug("outside RTH; idle")
        await asyncio.sleep(_sleep_until_next_5min())


def cli() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    p.add_argument("--force", action="store_true",
                   help="Ignore the RTH gate (debug; fetches even off-hours)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return asyncio.run(main(once=args.once, force=args.force))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(cli())
