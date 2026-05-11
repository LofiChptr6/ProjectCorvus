#!/usr/bin/env python
"""Embedding backfill sweeper.

Picks up news_items rows where `embedding IS NULL` (and pgvector is installed),
batch-embeds them, writes back via store.set_news_embedding. Idempotent — exit
fast when nothing pending.

Modes:
    python scripts/embed_news_sweeper.py                  # one normal pass (LIMIT 200)
    python scripts/embed_news_sweeper.py --batch 500      # bigger pass
    python scripts/embed_news_sweeper.py --backfill-all   # walk until empty
    python scripts/embed_news_sweeper.py -v               # verbose

Run by systemd timer trading-embed-sweeper.timer every 5 min — fast no-op when
nothing's pending (the idx_news_embed_pending partial index keeps the lookup
cheap).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
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

from data import embeddings
from db import store
from db.schema import get_pool

log = logging.getLogger("embed_news_sweeper")


async def _pending_count() -> int:
    """Return the count of news_items with NULL embedding (returns 0 if the
    column doesn't exist yet — pgvector not installed)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            return int(await conn.fetchval(
                "SELECT count(*) FROM news_items WHERE embedding IS NULL"
            ))
        except Exception:
            return 0


async def _pending_batch(batch_size: int) -> list[dict]:
    """Newest-first — fresh items embed before backlog."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                """SELECT article_id, headline, body
                   FROM news_items
                   WHERE embedding IS NULL
                     AND article_id IS NOT NULL
                   ORDER BY published_at DESC NULLS LAST, id DESC
                   LIMIT $1""",
                int(batch_size),
            )
        except Exception as exc:
            log.warning("pending fetch failed (pgvector installed?): %s", exc)
            return []
        return [dict(r) for r in rows]


async def sweep_once(batch_size: int) -> dict:
    """One sweep: fetch up to batch_size NULL-embedding rows, embed, write back."""
    rows = await _pending_batch(batch_size)
    if not rows:
        return {"fetched": 0, "embedded": 0, "failed": 0}
    texts = [
        embeddings.text_for_embedding(r.get("headline") or "", r.get("body") or "")
        for r in rows
    ]
    vecs = await embeddings.embed_batch(texts)
    model = embeddings.active_model_name() or "unknown"
    embedded = 0
    failed = 0
    for r, vec in zip(rows, vecs):
        if vec is None:
            failed += 1
            continue
        ok = await store.set_news_embedding(r["article_id"], vec, model)
        if ok:
            embedded += 1
        else:
            failed += 1
    return {"fetched": len(rows), "embedded": embedded, "failed": failed}


async def run(batch_size: int, backfill_all: bool) -> int:
    if not embeddings.provider_ready():
        log.error("no embedding provider configured (need VOYAGE_API_KEY or OPENAI_API_KEY) — exiting")
        return 2

    pending = await _pending_count()
    log.info("starting sweep — provider=%s pending=%d batch=%d backfill_all=%s",
             embeddings.active_model_name(), pending, batch_size, backfill_all)

    total = {"fetched": 0, "embedded": 0, "failed": 0}
    while True:
        r = await sweep_once(batch_size)
        for k in total:
            total[k] += r[k]
        if r["fetched"] < batch_size:
            # Drained, or nothing pending.
            break
        if not backfill_all:
            break
    await embeddings.aclose()
    log.info("done — fetched=%d embedded=%d failed=%d",
             total["fetched"], total["embedded"], total["failed"])
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--batch", type=int, default=200,
                   help="Rows per sweep iteration (default 200)")
    p.add_argument("--backfill-all", action="store_true",
                   help="Loop until no pending rows remain (one-shot historical fill)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return asyncio.run(run(args.batch, args.backfill_all))


if __name__ == "__main__":
    raise SystemExit(main())
