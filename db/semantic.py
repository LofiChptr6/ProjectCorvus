"""Time-decayed semantic recall over news_items (Phase B).

Single entrypoint:

    rows = await semantic_news_recall(
        query_text="OPEC production cuts and crude oversupply",
        agent_name=None,
        top_k=10,
        half_life_hours=24,
        symbol_filter=None,
        max_age_days=30,
    )

Scoring:
    sim   = cosine_similarity(query_embedding, row.embedding)  (HNSW-indexed)
    decay = exp( -ln(2) * hours_since_published / half_life_hours )
    score = sim * decay

Half-life recommendations:
    24h   — "what's hot right now" (default)
    168h  — "this week's themes"
    720h  — "this month's narrative"

Two-stage retrieval: HNSW pulls top-N raw similarity candidates (cheap, indexed),
then we re-rank in SQL by combined score. Pulling MAX_CANDIDATES > top_k lets an
older-but-very-relevant article beat a recent-but-loosely-related one.

Behavior when pgvector is not yet installed: returns [] with a single log warning.
Never raises out of this module.
"""

from __future__ import annotations

import logging
from typing import Optional

from db.schema import get_pool

log = logging.getLogger(__name__)

DEFAULT_HALF_LIFE_HOURS = 24.0
MAX_CANDIDATES = 50  # pull this many by raw similarity before re-ranking


def _vec_literal(vec: list[float]) -> str:
    """asyncpg can't auto-cast list[float] to pgvector — encode as a literal
    that we feed as text and cast with ::vector inside the SQL."""
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"


async def semantic_news_recall(
    query_text: str,
    agent_name: Optional[str] = None,
    top_k: int = 10,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
    symbol_filter: Optional[list[str]] = None,
    max_age_days: int = 30,
) -> list[dict]:
    """Return top_k rows by (similarity × time-decay) score.

    Args:
        query_text: Free-text query — gets embedded with the same provider as
            the row vectors.
        agent_name: Currently informational only (for logging); does not filter
            results. Per the plan, semantic recall is desk-wide ad-hoc.
        top_k: Number of results to return.
        half_life_hours: Decay constant.
        symbol_filter: Optional whitelist of tickers.
        max_age_days: Hard cutoff — older articles excluded from the candidate
            pool even if highly similar.
    """
    from data import embeddings  # late import — avoid loading on cold path
    if not embeddings.provider_ready():
        log.info("semantic_news_recall: no embedding provider configured; returning []")
        return []
    q_vec = await embeddings.embed_one(query_text)
    if q_vec is None:
        log.info("semantic_news_recall: query embed returned None; returning []")
        return []

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                """
                WITH cand AS (
                    SELECT id, symbol, headline, body, url, published_at,
                           fetched_at, provider, sentiment, channels, category,
                           importance, agent_tags,
                           1 - (embedding <=> $1::vector) AS sim,
                           EXTRACT(EPOCH FROM (NOW() - COALESCE(published_at, fetched_at::timestamptz))) / 3600.0
                               AS hours_old
                    FROM news_items
                    WHERE embedding IS NOT NULL
                      AND COALESCE(published_at, fetched_at::timestamptz)
                            > NOW() - ($3 || ' days')::interval
                      AND ($4::text[] IS NULL OR symbol = ANY($4::text[]))
                    ORDER BY embedding <=> $1::vector
                    LIMIT $2
                )
                SELECT *,
                       sim * exp( -ln(2.0) * hours_old / $5::float )::float AS score
                FROM cand
                ORDER BY score DESC
                LIMIT $6
                """,
                _vec_literal(q_vec),
                int(MAX_CANDIDATES),
                str(int(max_age_days)),
                symbol_filter,
                float(half_life_hours),
                int(top_k),
            )
        except Exception as exc:
            # pgvector not installed, column missing, type mismatch, etc.
            # Never raise — recall is best-effort.
            log.warning("semantic_news_recall: query failed (pgvector ready? %s): %s",
                        type(exc).__name__, exc)
            return []
        return [dict(r) for r in rows]
