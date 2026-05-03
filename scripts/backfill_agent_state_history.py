"""Backfill `agent_state` hourly snapshots over the historical window
[earliest agent_ledger event, now] using Polygon historical hourly bars.

Run AFTER `scripts/backfill_ledger_from_fills.py` so the ledger is populated.

Approach:
  1. Find all distinct symbols ever held by any agent.
  2. Pull hourly bars for each symbol once (one Polygon call per symbol
     spanning the full window).
  3. Stream-replay agent_ledger events in chronological order, maintaining
     per-(agent, symbol) running qty + weighted-avg cost.
  4. At each hour bucket, snapshot the state — mark each open position to
     the nearest hourly bar's close, compute realized + unrealized, UPSERT
     one row per agent.

Idempotent (UPSERT on agent_state.(agent_name, hour_bucket)).

Run:
    .venv/bin/python -m scripts.backfill_agent_state_history
    .venv/bin/python -m scripts.backfill_agent_state_history --since 7d
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env", override=False)
except ImportError:
    pass

log = logging.getLogger("backfill_agent_state")


# ── Polygon hourly bars (one call per symbol over full window) ──────────────

async def _fetch_hourly_bars(
    symbols: list[str],
    days_back: int,
) -> dict[str, list[tuple[datetime, float]]]:
    """Return {symbol: sorted [(ts_utc, close), ...]} over the lookback window."""
    from data.massive_client import get_bars

    duration = f"{max(days_back, 1)} D"

    async def _one(sym: str) -> tuple[str, list[tuple[datetime, float]]]:
        try:
            data = await get_bars(sym, bar_size="1 hour", duration=duration)
        except Exception as exc:
            log.warning("get_bars(%s) failed: %s", sym, exc)
            return sym, []
        bars = []
        for b in data.get("bars") or []:
            t_iso = b.get("t")
            c = b.get("c")
            if not t_iso or c is None:
                continue
            try:
                ts = datetime.fromisoformat(t_iso.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            bars.append((ts, float(c)))
        bars.sort(key=lambda x: x[0])
        return sym, bars

    pairs = await asyncio.gather(*[_one(s) for s in symbols])
    return dict(pairs)


def _mark_at(bars: list[tuple[datetime, float]], at: datetime) -> Optional[float]:
    """Latest close at-or-before `at`. Binary search; returns None if no bar."""
    if not bars:
        return None
    # bars sorted ascending — linear walk is fine for our scale (~hourly buckets).
    last = None
    for ts, c in bars:
        if ts <= at:
            last = c
        else:
            break
    return last


# ── Hour-bucket iteration ───────────────────────────────────────────────────

def _hour_buckets(start: datetime, end: datetime) -> list[datetime]:
    """Inclusive hourly stamps from `start` (rounded UP to next hour) through
    `end` (rounded DOWN). Each stamp is the END of that hour bucket — which
    is what we snapshot at."""
    s = start.replace(minute=0, second=0, microsecond=0)
    if s < start:
        s += timedelta(hours=1)
    e = end.replace(minute=0, second=0, microsecond=0)
    out: list[datetime] = []
    cur = s
    while cur <= e:
        out.append(cur)
        cur += timedelta(hours=1)
    return out


# ── Replay + snapshot ───────────────────────────────────────────────────────

async def backfill(since_arg: Optional[str] = None) -> dict:
    from db.schema import get_pool
    from db import store as _store

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 1. Pull all ledger events sorted ascending
        events = await conn.fetch(
            """SELECT id, booked_at, agent_name, symbol, event,
                      qty::float8 AS qty,
                      price_per_share::float8 AS price,
                      realized_pnl::float8 AS realized_pnl
               FROM agent_ledger
               ORDER BY booked_at, id"""
        )
        if not events:
            log.info("agent_ledger is empty — run backfill_ledger_from_fills first")
            return {"hours_written": 0, "rows_written": 0}

        first_event_at = events[0]["booked_at"]
        now = datetime.now(timezone.utc)
        if since_arg:
            from datetime import timedelta
            if since_arg.endswith("d"):
                start = max(first_event_at, now - timedelta(days=int(since_arg[:-1])))
            elif since_arg.endswith("h"):
                start = max(first_event_at, now - timedelta(hours=int(since_arg[:-1])))
            else:
                start = first_event_at
        else:
            start = first_event_at

        symbols = sorted({e["symbol"].upper() for e in events})
        days_back = max(1, (now - first_event_at).days + 2)

    log.info("fetching hourly bars for %d symbols (≈%d days)…", len(symbols), days_back)
    bars_by_sym = await _fetch_hourly_bars(symbols, days_back)
    n_with_bars = sum(1 for s in symbols if bars_by_sym.get(s))
    log.info("got bars for %d/%d symbols", n_with_bars, len(symbols))

    # 2. Streaming replay: maintain per-(agent, symbol) running state and
    # per-agent cumulative realized.
    state: dict[tuple, dict] = {}    # (agent, sym) → {qty, total_cost}
    realized: dict[str, float] = {}

    def apply_event(e):
        key = (e["agent_name"], e["symbol"].upper())
        st = state.setdefault(key, {"qty": 0.0, "cost": 0.0})
        ev = e["event"]
        q = float(e["qty"] or 0)
        p = float(e["price"] or 0)
        if ev == "LEND":
            st["qty"] += q
            st["cost"] += q * p
        elif ev == "RETURN":
            avg = (st["cost"] / st["qty"]) if st["qty"] > 1e-9 else 0.0
            st["qty"] -= q
            st["cost"] -= q * avg
            if e["realized_pnl"] is not None:
                realized[e["agent_name"]] = realized.get(e["agent_name"], 0.0) + float(e["realized_pnl"])
            if st["qty"] < 1e-9:
                st["qty"] = 0.0
                st["cost"] = 0.0
        elif ev == "DIVIDEND":
            if e["realized_pnl"] is not None:
                realized[e["agent_name"]] = realized.get(e["agent_name"], 0.0) + float(e["realized_pnl"])

    buckets = _hour_buckets(start, now)
    log.info("walking %d hour buckets from %s to %s",
             len(buckets), start.isoformat(), now.isoformat())

    rows_written = 0
    hours_with_data = 0
    i = 0  # event cursor
    for bucket in buckets:
        # Apply all events strictly at-or-before bucket
        while i < len(events) and events[i]["booked_at"] <= bucket:
            apply_event(events[i])
            i += 1

        # Snapshot every agent that has had any activity by this point
        all_agents = set(realized.keys()) | {a for (a, _) in state.keys()}
        if not all_agents:
            continue

        snap_rows: list[dict] = []
        for agent in sorted(all_agents):
            unreal = 0.0
            open_cost = 0.0
            open_mv = 0.0
            positions_json: list[dict] = []
            for (a, sym), st in state.items():
                if a != agent or st["qty"] <= 1e-9:
                    continue
                avg = st["cost"] / st["qty"]
                bars = bars_by_sym.get(sym, [])
                mark = _mark_at(bars, bucket) or 0.0
                mv = st["qty"] * mark
                unr = st["qty"] * (mark - avg) if mark > 0 else 0.0
                open_cost += st["qty"] * avg
                open_mv += mv
                unreal += unr
                positions_json.append({
                    "sym": sym,
                    "qty": round(st["qty"], 6),
                    "avg_cost": round(avg, 4),
                    "mark": round(mark, 4),
                    "market_value": round(mv, 2),
                    "unrealized": round(unr, 2),
                })
            r = realized.get(agent, 0.0)
            snap_rows.append({
                "agent_name": agent,
                "realized_pnl": r,
                "unrealized_pnl": unreal,
                "total_pnl": r + unreal,
                "open_cost": open_cost,
                "open_market_value": open_mv,
                "n_positions": len(positions_json),
                "positions_json": positions_json,
            })

        # UPSERT with explicit snapshot_at (= bucket end)
        async with pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO agent_state
                     (snapshot_at, agent_name, realized_pnl, unrealized_pnl,
                      total_pnl, open_cost, open_market_value, n_positions,
                      positions_json)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
                   ON CONFLICT (agent_name, hour_bucket) DO UPDATE SET
                     snapshot_at       = EXCLUDED.snapshot_at,
                     realized_pnl      = EXCLUDED.realized_pnl,
                     unrealized_pnl    = EXCLUDED.unrealized_pnl,
                     total_pnl         = EXCLUDED.total_pnl,
                     open_cost         = EXCLUDED.open_cost,
                     open_market_value = EXCLUDED.open_market_value,
                     n_positions       = EXCLUDED.n_positions,
                     positions_json    = EXCLUDED.positions_json""",
                [(bucket, r["agent_name"], r["realized_pnl"], r["unrealized_pnl"],
                  r["total_pnl"], r["open_cost"], r["open_market_value"],
                  r["n_positions"], json.dumps(r["positions_json"]))
                 for r in snap_rows],
            )
        rows_written += len(snap_rows)
        hours_with_data += 1

    return {
        "hours_written": hours_with_data,
        "rows_written": rows_written,
        "n_agents": len({(a, s)[0] for (a, s) in state.keys()}),
        "first_bucket": buckets[0].isoformat() if buckets else None,
        "last_bucket": buckets[-1].isoformat() if buckets else None,
    }


async def _main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", help="window: '7d', '24h', or omit for full ledger range")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    summary = await backfill(since_arg=args.since)
    log.info("backfill_agent_state complete: %s", summary)
    print(f"wrote {summary['rows_written']} agent_state rows over "
          f"{summary['hours_written']} hour buckets")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
