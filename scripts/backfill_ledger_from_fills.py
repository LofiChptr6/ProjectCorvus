"""One-shot backfill: replay every row in `fills` chronologically through
`book_fill_to_ledger`, reconstructing `agent_ledger` LEND/RETURN events with
their original timestamps.

Why this is needed: the migrate_to_ledger script dropped agent_pnl_attribution
(hard reset). The fills table still has the full broker history. Replaying it
through the live fill→ledger writer produces the equivalent agent_ledger,
preserving the same conviction-driven attribution that the live path uses.

Idempotent: clears agent_ledger before replay so re-runs are safe.

Run:
    .venv/bin/python -m scripts.backfill_ledger_from_fills
    .venv/bin/python -m scripts.backfill_ledger_from_fills --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env", override=False)
except ImportError:
    pass

log = logging.getLogger("backfill_ledger")


async def replay() -> dict:
    from db.schema import get_pool
    from meta_agent.ledger_writer import book_fill_to_ledger

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Clear ledger for a clean replay
        n_existing = await conn.fetchval("SELECT COUNT(*) FROM agent_ledger")
        if n_existing:
            log.info("clearing %d existing agent_ledger rows", n_existing)
            await conn.execute("DELETE FROM agent_ledger")

        # Pull fills in chronological order. Each fill has order_id which is
        # what book_fill_to_ledger uses to find the originating decision.
        fills = await conn.fetch(
            """SELECT id, order_id, agent_name, filled_at, symbol, action,
                      quantity::float8 AS quantity,
                      fill_price::float8 AS fill_price
               FROM fills
               ORDER BY filled_at::timestamptz, id"""
        )

    log.info("replaying %d fills…", len(fills))

    counts = {"LEND": 0, "RETURN": 0, "ORPHAN": 0}
    rows_written = 0
    for f in fills:
        # Parse the fill timestamp for the booked_at override.
        from datetime import datetime, timezone
        s = f["filled_at"]
        try:
            ts = datetime.fromisoformat(s.replace("Z", "+00:00")) if isinstance(s, str) else s
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception as exc:
            log.warning("could not parse filled_at=%r for fill %s: %s; skipping", s, f["id"], exc)
            continue

        try:
            res = await book_fill_to_ledger(
                fill_id=f["id"],
                order_id=f["order_id"],
                symbol=f["symbol"],
                action=f["action"],
                quantity=float(f["quantity"]),
                fill_price=float(f["fill_price"]),
                booked_at=ts,
            )
        except Exception as exc:
            log.warning("fill %s blew up: %s", f["id"], exc)
            continue

        ev = res.get("event", "ORPHAN")
        counts[ev] = counts.get(ev, 0) + 1
        rows_written += res.get("rows_written", 0)

    summary = {
        "fills_replayed": len(fills),
        "ledger_rows_written": rows_written,
        "events_by_type": counts,
    }
    return summary


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would happen, no DB writes")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.dry_run:
        from db.schema import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            n_fills = await conn.fetchval("SELECT COUNT(*) FROM fills")
            n_ledger = await conn.fetchval("SELECT COUNT(*) FROM agent_ledger")
            print(f"dry-run: would clear {n_ledger} agent_ledger rows and replay {n_fills} fills")
        return 0

    summary = await replay()
    log.info("Backfill complete: %s", summary)
    print(f"replayed {summary['fills_replayed']} fills → "
          f"{summary['ledger_rows_written']} ledger rows "
          f"({summary['events_by_type']})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
