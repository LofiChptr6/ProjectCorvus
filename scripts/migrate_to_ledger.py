"""One-shot migration: drop the old kanban / attribution / positions_snapshot
/ pnl_daily tables and create the new agent_ledger + agent_state tables.

This is a HARD RESET — by user decision, no historical P&L is preserved.
Open IBKR positions remain at IBKR; they become orphan to mike (i.e. not lent
to any agent) until mike's first post-deploy rebalance writes new LEND events.

Run once after deploying the redesign:
    .venv/bin/python -m scripts.migrate_to_ledger

Idempotent: re-running after migration is a no-op (DROP IF EXISTS, then init_db
which uses CREATE IF NOT EXISTS).
"""

from __future__ import annotations

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


log = logging.getLogger("migrate_to_ledger")


# Drop in dependency order — child references first.
DROP_STATEMENTS = [
    # holding_kanban referenced allocation_decision via FK; drop kanban first
    "DROP TABLE IF EXISTS holding_kanban CASCADE",
    "DROP TABLE IF EXISTS agent_pnl_attribution CASCADE",
    "DROP TABLE IF EXISTS positions_snapshot CASCADE",
    "DROP TABLE IF EXISTS pnl_daily CASCADE",
]


async def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from db.schema import get_pool, init_db

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 1. Show what's about to die
        for tbl in ('holding_kanban', 'agent_pnl_attribution',
                    'positions_snapshot', 'pnl_daily'):
            n = await conn.fetchval(
                f"SELECT COUNT(*) FROM {tbl}"
            ) if await conn.fetchval(f"SELECT to_regclass('public.{tbl}')") else 0
            log.info("  %s: %s rows", tbl, n)

        # 2. Drop old tables
        for stmt in DROP_STATEMENTS:
            log.info("  %s", stmt)
            await conn.execute(stmt)

    # 3. Create new tables (and any retained ones) via the standard init.
    await init_db()

    # 4. Verify
    async with pool.acquire() as conn:
        for tbl in ('agent_ledger', 'agent_state', 'nav_log',
                    'positions_anchor', 'fills', 'allocation_decision'):
            exists = await conn.fetchval(f"SELECT to_regclass('public.{tbl}')")
            log.info("  %s: %s", tbl, exists)

        for tbl in ('holding_kanban', 'agent_pnl_attribution',
                    'positions_snapshot', 'pnl_daily'):
            exists = await conn.fetchval(f"SELECT to_regclass('public.{tbl}')")
            assert exists is None, f"{tbl} still exists after migration"
            log.info("  %s: dropped (verified)", tbl)

    log.info("Migration complete. Mike's first post-deploy rebalance will seed agent_ledger.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
