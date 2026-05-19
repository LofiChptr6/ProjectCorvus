"""Per-symbol news feature extraction for quant models.

Aggregates `news_items` over a recency window into a fixed-shape feature
vector that probabilistic models consume via `context["news_features"]`.
Persisted to `news_features` table so 10 agents × ~30 symbols × hourly
reviews don't re-scan news_items thousands of times an hour.

Feature schema (closed):
    {
        "count_earnings":                  int,
        "count_guidance":                  int,
        "count_m_and_a":                   int,
        "count_regulatory":                int,
        "count_analyst_ratings":           int,
        "count_other":                     int,
        "recency_weighted_sentiment":      float,   # signed in [-1, 1]
        "time_since_last_high_importance_min": Optional[float],  # minutes
        "max_importance_score":            float,   # 0 / 0.5 / 1
        "window_minutes":                  int,
        "computed_at":                     str,     # ISO-8601
    }

Sentiment mapping (Benzinga's `sentiment` column is free-text):
    "positive" / "bullish"  → +1
    "negative" / "bearish"  → -1
    everything else         →  0
Each article's sentiment is then weighted by exp(-age_minutes / half_life).
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

CATEGORY_BUCKETS = {
    "count_earnings":        {"earnings"},
    "count_guidance":        {"guidance"},
    "count_m_and_a":         {"m_and_a"},
    "count_regulatory":      {"regulatory"},
    "count_analyst_ratings": {"analyst_ratings"},
}
HIGH_IMPORTANCE = {"high"}
_POS_SENT = {"positive", "bullish"}
_NEG_SENT = {"negative", "bearish"}


def _sentiment_score(raw: Optional[str]) -> float:
    if raw is None:
        return 0.0
    s = raw.strip().lower()
    if s in _POS_SENT:
        return 1.0
    if s in _NEG_SENT:
        return -1.0
    return 0.0


def _importance_score(raw: Optional[str]) -> float:
    if raw is None:
        return 0.0
    s = raw.strip().lower()
    if s in HIGH_IMPORTANCE:
        return 1.0
    if s == "normal":
        return 0.5
    return 0.0


async def compute_news_features(
    symbol: str,
    window_minutes: int = 240,
    sentiment_half_life_minutes: float = 60.0,
) -> dict:
    """Pull news_items for `symbol` within the last `window_minutes` (based on
    published_at; fetched_at fallback when published_at is NULL) and aggregate
    into the feature vector. Recency weight is exp(-age/half_life)."""
    from db.schema import get_pool
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=window_minutes)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT category, importance, sentiment,
                      COALESCE(published_at, fetched_at::timestamptz) AS ts
               FROM news_items
               WHERE symbol = $1
                 AND COALESCE(published_at, fetched_at::timestamptz) >= $2""",
            symbol.upper(), cutoff,
        )

    counts = {k: 0 for k in CATEGORY_BUCKETS}
    counts["count_other"] = 0
    weighted_sent_num = 0.0
    weighted_sent_den = 0.0
    last_high_ts: Optional[datetime] = None
    max_importance = 0.0

    for r in rows:
        cat = (r["category"] or "general").strip().lower()
        bucketed = False
        for key, members in CATEGORY_BUCKETS.items():
            if cat in members:
                counts[key] += 1
                bucketed = True
                break
        if not bucketed:
            counts["count_other"] += 1

        imp_score = _importance_score(r["importance"])
        if imp_score > max_importance:
            max_importance = imp_score
        if imp_score >= 1.0:
            ts = r["ts"]
            if isinstance(ts, datetime):
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if last_high_ts is None or ts > last_high_ts:
                    last_high_ts = ts

        ts = r["ts"]
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_min = max(0.0, (now - ts).total_seconds() / 60.0)
            weight = math.exp(-age_min / max(sentiment_half_life_minutes, 1.0))
            weighted_sent_num += weight * _sentiment_score(r["sentiment"])
            weighted_sent_den += weight

    rws = (weighted_sent_num / weighted_sent_den) if weighted_sent_den > 0 else 0.0
    tsl: Optional[float] = None
    if last_high_ts is not None:
        tsl = max(0.0, (now - last_high_ts).total_seconds() / 60.0)

    return {
        **counts,
        "recency_weighted_sentiment": round(rws, 4),
        "time_since_last_high_importance_min": tsl,
        "max_importance_score": max_importance,
        "window_minutes": window_minutes,
        "computed_at": now.isoformat(),
    }


async def snapshot_symbols(symbols: list[str], window_minutes: int = 240) -> dict:
    """Compute + persist features for each symbol. Used by the periodic
    precompute job. Returns {symbol: payload}."""
    from db import store
    out: dict[str, dict] = {}
    for sym in symbols:
        payload = await compute_news_features(sym, window_minutes=window_minutes)
        await store.upsert_news_features(sym, window_minutes, payload)
        out[sym] = payload
    return out


def empty_features(window_minutes: int = 240) -> dict:
    """Default feature vector when no snapshot exists yet — same shape so model
    code can read fields unconditionally."""
    return {
        "count_earnings":        0,
        "count_guidance":        0,
        "count_m_and_a":         0,
        "count_regulatory":      0,
        "count_analyst_ratings": 0,
        "count_other":           0,
        "recency_weighted_sentiment": 0.0,
        "time_since_last_high_importance_min": None,
        "max_importance_score":  0.0,
        "window_minutes":        window_minutes,
        "computed_at":           datetime.now(timezone.utc).isoformat(),
    }
