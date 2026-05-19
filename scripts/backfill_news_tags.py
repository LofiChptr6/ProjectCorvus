#!/usr/bin/env python
"""One-shot backfill: re-apply (category, importance, agent_tags) to every
news_items row.

The ingestor's `ON CONFLICT DO UPDATE` only re-tags articles whose article_id
shows up in a later Massive query — but most old rows roll off Massive's
recent-news window and never get touched again. This script walks the entire
table once and re-runs the tagger so:

  - Old rows ingested before the Phase A columns existed get retroactively
    tagged (e.g. META/NVDA articles that predate the column add).
  - Rows mis-categorized by old/buggy channel maps get re-categorized.

Idempotent: safe to re-run after tweaking the channel map or sector_map.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from db.schema import get_pool
from scripts.ingest_news import (
    categorize, importance_for, macro_tags_for, collect_target_tickers,
)

log = logging.getLogger("backfill_news_tags")


async def run(only_null: bool, dry_run: bool) -> int:
    _all_symbols, symbol_to_agents = await collect_target_tickers()
    log.info("loaded watchlist mapping: %d symbols", len(symbol_to_agents))

    pool = await get_pool()
    async with pool.acquire() as conn:
        if only_null:
            rows = await conn.fetch(
                "SELECT id, symbol, headline, channels, category, importance, agent_tags "
                "FROM news_items WHERE category IS NULL OR agent_tags IS NULL"
            )
        else:
            rows = await conn.fetch(
                "SELECT id, symbol, headline, channels, category, importance, agent_tags "
                "FROM news_items"
            )

    log.info("scanning %d rows (only_null=%s)", len(rows), only_null)
    n_cat_changed = 0
    n_tag_changed = 0
    n_updated = 0
    samples: list[str] = []

    pool = await get_pool()
    async with pool.acquire() as conn:
        for r in rows:
            sym = (r["symbol"] or "").upper()
            channels = r["channels"] or []
            headline = r["headline"] or ""

            new_cat = categorize(channels, headline)
            new_imp = importance_for(new_cat)
            agent_set: set[str] = set()
            if sym:
                agent_set.update(symbol_to_agents.get(sym, set()))
            if not agent_set:
                agent_set.update(macro_tags_for(headline))
            new_tags = sorted(agent_set) or None

            old_cat = r["category"]
            old_imp = r["importance"]
            old_tags = list(r["agent_tags"]) if r["agent_tags"] else None

            changed = (new_cat != old_cat) or (new_imp != old_imp) or (new_tags != old_tags)
            if not changed:
                continue
            if new_cat != old_cat:
                n_cat_changed += 1
            if new_tags != old_tags:
                n_tag_changed += 1
            if len(samples) < 5:
                samples.append(
                    f"id={r['id']} sym={sym} | cat: {old_cat}→{new_cat} | "
                    f"tags: {old_tags}→{new_tags}"
                )
            if not dry_run:
                await conn.execute(
                    "UPDATE news_items SET category=$1, importance=$2, agent_tags=$3 WHERE id=$4",
                    new_cat, new_imp, new_tags, r["id"],
                )
                n_updated += 1

    log.info("category changes: %d", n_cat_changed)
    log.info("agent_tags changes: %d", n_tag_changed)
    log.info("rows updated: %d%s", n_updated, " (dry-run, no writes)" if dry_run else "")
    for s in samples:
        log.info("  sample: %s", s)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--only-null", action="store_true",
                   help="Only process rows where category IS NULL or agent_tags IS NULL")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute changes but don't UPDATE anything")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return asyncio.run(run(only_null=args.only_null, dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
