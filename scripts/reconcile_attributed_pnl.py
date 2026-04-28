"""FIFO reconciler for agent_pnl_attribution.attributed_pnl.

The live `_on_fill` path (`ibkr/orders.py`) calls
`meta_agent.pnl_attribution.reconcile_symbol` after every fill, which
keeps attribution accurate in real time. This script runs the same
function across every symbol that has ever traded — useful for one-shot
backfills, after restoring from backup, or as a nightly safety net via
systemd.

Idempotent: rows already carrying a non-NULL `attributed_pnl` are
skipped.

Usage:
    python -m scripts.reconcile_attributed_pnl                 # all history
    python -m scripts.reconcile_attributed_pnl --since 2026-04-01
    python -m scripts.reconcile_attributed_pnl --symbol AAPL
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reconcile_attributed_pnl")


async def _all_symbols(since: str | None) -> list[str]:
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        if since:
            rows = await conn.fetch(
                "SELECT DISTINCT symbol FROM fills WHERE filled_at >= $1 ORDER BY symbol",
                since,
            )
        else:
            rows = await conn.fetch("SELECT DISTINCT symbol FROM fills ORDER BY symbol")
    return [r["symbol"] for r in rows if r["symbol"]]


async def main(since: str | None, symbol: str | None) -> None:
    from db.schema import close_pool
    from meta_agent.pnl_attribution import reconcile_symbol

    symbols = [symbol.upper()] if symbol else await _all_symbols(since)
    log.info("reconciling %d symbol(s)", len(symbols))

    totals = {"events": 0, "rows_updated": 0, "skipped_already_set": 0, "skipped_no_rows": 0}
    for sym in symbols:
        res = await reconcile_symbol(sym, since=since)
        for k in totals:
            totals[k] += res.get(k, 0)
        if res["events"]:
            log.info(
                "  %s: events=%d updated=%d already=%d no_rows=%d",
                sym, res["events"], res["rows_updated"],
                res["skipped_already_set"], res["skipped_no_rows"],
            )

    log.info("done: %s", totals)
    await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default=None, help="ISO date/time floor (e.g. 2026-04-01)")
    parser.add_argument("--symbol", default=None, help="Single symbol (default: all)")
    args = parser.parse_args()
    asyncio.run(main(since=args.since, symbol=args.symbol))
