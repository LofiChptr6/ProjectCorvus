"""One-shot migration: copy all rows from data/trading.db (SQLite) into Postgres.

Run once after switching the app over to Postgres. Idempotent — safe to re-run;
rows with unique keys (ibkr_exec_id, agent_name, trade_date+agent_name) will be
skipped on conflict. Rows without unique constraints get re-inserted, so don't
run it repeatedly if you care about audit_log / kill_switch duplication.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from db.schema import get_pool, init_db

SQLITE_PATH = Path("data/trading.db")


TABLES = [
    "audit_log",
    "tool_calls",
    "orders",
    "fills",
    "positions_snapshot",
    "pnl_daily",
    "agent_allocations",
    "kill_switch",
    "news_items",
]


async def migrate() -> None:
    if not SQLITE_PATH.exists():
        print(f"No sqlite file at {SQLITE_PATH} — nothing to migrate.")
        return

    await init_db()  # make sure pg schema is ready
    pool = await get_pool()

    con = sqlite3.connect(str(SQLITE_PATH))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    async with pool.acquire() as pg:
        for table in TABLES:
            try:
                rows = [dict(r) for r in cur.execute(f"SELECT * FROM {table}")]
            except sqlite3.OperationalError:
                print(f"[skip] {table} not present in sqlite")
                continue
            if not rows:
                print(f"[skip] {table} empty")
                continue

            # Drop 'id' so Postgres BIGSERIAL assigns fresh IDs (avoids conflicts
            # if pg already has rows). Matches source order by rowid.
            cols = [c for c in rows[0].keys() if c != "id"]
            placeholders = ",".join(f"${i+1}" for i in range(len(cols)))
            col_list = ",".join(cols)

            conflict = ""
            if table == "fills":
                conflict = " ON CONFLICT (ibkr_exec_id) DO NOTHING"
            elif table == "agent_allocations":
                conflict = " ON CONFLICT (agent_name) DO NOTHING"
            elif table == "pnl_daily":
                conflict = " ON CONFLICT (trade_date, agent_name) DO NOTHING"

            sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}){conflict}"

            inserted = 0
            for r in rows:
                values = [r[c] for c in cols]
                await pg.execute(sql, *values)
                inserted += 1
            print(f"[ok]   {table}: {inserted} rows")

    con.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
