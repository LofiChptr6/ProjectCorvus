#!/usr/bin/env python
"""Periodic news ingestor — Massive (Benzinga add-on) → Postgres + threads board.

Run by systemd timer trading-news-ingest.timer every 10 min on weekdays. Two-
sink writer:
  1. INSERT into `news_items` with category + importance + agent_tags columns
     (snapshotted at ingest from sector_map, so dashboard + per-agent context
     queries don't re-parse YAML on every read).
  2. POST to the `news-headlines` thread (`post` table) with the same metadata
     in `meta` JSONB so the existing thread MCP tools (`get_thread_posts`,
     `search_posts`) Just Work over news.

Categorization rules live as module-level constants below; they're channel-
list first (Benzinga slugs), regex-on-headline second, then 'general'.

Manual:
    python scripts/ingest_news.py
    python scripts/ingest_news.py --symbols AAPL,SPY,VIX --max-items 20
    python scripts/ingest_news.py --skip-market-pull
    python scripts/ingest_news.py --rth-only          # exit early outside RTH
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

# Allow `python scripts/ingest_news.py` from anywhere — repo root on sys.path.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load .env so systemd ExecStart picks up MASSIVE_API_KEY etc.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from data import embeddings, massive_client
from db import store

log = logging.getLogger("ingest_news")

# ── Watchlist → agent tags ────────────────────────────────────────────────────

async def collect_target_tickers() -> tuple[set[str], dict[str, set[str]]]:
    """Returns (all_symbols, symbol_to_agents) from agent_watchlist (SQL).

    The unified agent_watchlist table holds both seed rows (mirrored from
    sector_map.yaml at first boot) and live agent/user additions; this
    single read replaces the previous sector_map.yaml + per-agent
    watchlist.md union.
    """
    grouped = await store.load_all_watchlists()
    out: dict[str, set[str]] = {}
    for agent_name, rows in grouped.items():
        for r in rows:
            sym_u = str(r["symbol"]).upper()
            out.setdefault(sym_u, set()).add(agent_name)
    all_symbols = set(out.keys())
    return all_symbols, out


# ── Categorization ────────────────────────────────────────────────────────────

# Categories the dashboard + agent context treat as high-importance. Earnings
# and M&A drive the biggest single-name moves; guidance changes (raise/cut)
# are often as market-moving as earnings on small/mid caps.
IMPORTANT_CATEGORIES = {"earnings", "m_and_a", "guidance"}

# Benzinga channel-slug → canonical category. Comparison is lowercased.
# Channels arrive lowercased + space-separated from Benzinga; cover singular,
# plural, and quarter-templated variants since publishers are inconsistent.
_CHANNEL_TO_CATEGORY: dict[str, str] = {
    # Earnings (any framing)
    "earnings": "earnings",
    "earnings beat": "earnings",
    "earnings beats": "earnings",
    "earnings miss": "earnings",
    "earnings misses": "earnings",
    "earnings call": "earnings",
    "earnings calls": "earnings",
    "earnings growth": "earnings",
    "earnings report": "earnings",
    "earnings reports": "earnings",
    "earnings season": "earnings",
    "quarterly results": "earnings",
    "quarterly earnings": "earnings",
    "q1 earnings": "earnings",
    "q2 earnings": "earnings",
    "q3 earnings": "earnings",
    "q4 earnings": "earnings",
    # Guidance
    "guidance": "guidance",
    "guidance raise": "guidance",
    "guidance cut": "guidance",
    "guidance update": "guidance",
    "outlook": "guidance",
    "forecast": "guidance",
    "annual guidance": "guidance",
    # M&A
    "m&a": "m_and_a",
    "mergers": "m_and_a",
    "merger": "m_and_a",
    "mergers & acquisitions": "m_and_a",
    "mergers and acquisitions": "m_and_a",
    "acquisition": "m_and_a",
    "acquisitions": "m_and_a",
    "takeover": "m_and_a",
    "buyout": "m_and_a",
    "spinoff": "m_and_a",
    "spin-off": "m_and_a",
    # Buybacks
    "buybacks": "buybacks",
    "buyback": "buybacks",
    "share buyback": "buybacks",
    "stock buyback": "buybacks",
    "share repurchase": "buybacks",
    # Dividends
    "dividends": "dividends",
    "dividend": "dividends",
    "dividend increase": "dividends",
    "dividend cut": "dividends",
    "special dividend": "dividends",
    # Analyst ratings (lower-importance but useful)
    "analyst ratings": "analyst_ratings",
    "analyst rating": "analyst_ratings",
    "downgrades": "analyst_ratings",
    "downgrade": "analyst_ratings",
    "upgrades": "analyst_ratings",
    "upgrade": "analyst_ratings",
    "price targets": "analyst_ratings",
    "price target": "analyst_ratings",
    "initiated coverage": "analyst_ratings",
    # Regulatory
    "fda": "regulatory",
    "fda approval": "regulatory",
    "fda rejection": "regulatory",
    "ema": "regulatory",
    "sec filings": "regulatory",
    "regulatory": "regulatory",
    "antitrust": "regulatory",
    "securities class action": "regulatory",
    "class action lawsuit": "regulatory",
    "securities fraud": "regulatory",
    # IPO / capital markets
    "ipos": "ipo",
    "ipo": "ipo",
    "secondary offering": "ipo",
    "follow-on offering": "ipo",
    "direct listing": "ipo",
    # Insider / 13F
    "insider trading": "insider",
    "insider buying": "insider",
    "insider selling": "insider",
    "13f": "insider",
    "13d": "insider",
}

# Headline regex fallback when channels are absent or generic. Order matters —
# higher-importance categories first so they win on multi-match.
_HEADLINE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:to\s+acquire|acquires|acquisition\s+of|merger|merging\s+with|takeover|buyout)\b", re.I), "m_and_a"),
    (re.compile(r"\b(?:Q[1-4]|quarterly|annual)\s+(?:earnings|results|revenue)\b", re.I), "earnings"),
    (re.compile(r"\b(?:earnings|EPS|revenue)\s+(?:beat|miss|topped?|missed)\b", re.I), "earnings"),
    (re.compile(r"\bguid(?:ance|es?)\s+(?:up|down|higher|lower|raised|cut|narrowed|widened)\b", re.I), "guidance"),
    (re.compile(r"\b(?:raises?|cuts?|lowers?|withdraws?)\s+(?:FY|full-year|fiscal|Q[1-4])\s+(?:guidance|outlook|forecast)\b", re.I), "guidance"),
    (re.compile(r"\b(?:upgrade[sd]?|downgrade[sd]?)\b(?!\s+to\s+Windows)", re.I), "analyst_ratings"),
    (re.compile(r"\bprice\s+target\b", re.I), "analyst_ratings"),
    (re.compile(r"\b(?:FDA|EMA)\s+(?:approves?|grants?|rejects?|declines?)\b", re.I), "regulatory"),
    (re.compile(r"\bstock\s+(?:buyback|repurchase)\b", re.I), "buybacks"),
    (re.compile(r"\bIPO\b", re.I), "ipo"),
]


def categorize(channels: Optional[list[str]], headline: str) -> str:
    """Return canonical category or 'general'. Channels first, regex fallback."""
    for ch in channels or []:
        slug = str(ch or "").strip().lower()
        if slug in _CHANNEL_TO_CATEGORY:
            return _CHANNEL_TO_CATEGORY[slug]
    for pat, cat in _HEADLINE_PATTERNS:
        if pat.search(headline or ""):
            return cat
    return "general"


def importance_for(category: str) -> str:
    return "high" if category in IMPORTANT_CATEGORIES else "normal"


# ── Macro fallback for untagged headlines ─────────────────────────────────────

# When a market-wide pull returns an article with no tickers attached, route
# it to the agent whose remit covers the macro driver. Kept intentionally
# small — extend iteratively as we see what flows through.
_MACRO_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:FOMC|Fed|Federal\s+Reserve|Powell|rate\s+(?:hike|cut|decision)|CPI|PCE|NFP|jobs\s+report|GDP|ISM|PMI|inflation|unemployment)\b", re.I), "atlas"),
    (re.compile(r"\b(?:dollar|DXY|treasury\s+yield|10-?year)\b", re.I), "atlas"),
    (re.compile(r"\b(?:OPEC|crude|oil\s+price|Brent|WTI|EIA\s+report|gasoline)\b", re.I), "energy"),
    (re.compile(r"\b(?:gold|silver|copper|iron\s+ore|aluminum|lithium)\b", re.I), "commodity"),
]


def macro_tags_for(headline: str) -> set[str]:
    out: set[str] = set()
    for pat, agent in _MACRO_PATTERNS:
        if pat.search(headline or ""):
            out.add(agent)
    return out


def _normalize_sentiment(raw: object) -> Optional[str]:
    """Coerce Benzinga's mixed sentiment ('Positive'/'Negative'/'Neutral' string
    OR numeric -1..1) to {'positive','negative','neutral',None}."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        if v > 0.15:
            return "positive"
        if v < -0.15:
            return "negative"
        return "neutral"
    s = str(raw).strip().lower()
    if s in ("positive", "bullish", "bull"):
        return "positive"
    if s in ("negative", "bearish", "bear"):
        return "negative"
    if s in ("neutral", "mixed"):
        return "neutral"
    return None


# ── Ingestion ─────────────────────────────────────────────────────────────────

async def _write_article(
    h: dict,
    symbol_to_agents: dict[str, set[str]],
    fallback_symbol: Optional[str] = None,
) -> tuple[bool, bool, bool]:
    """Process one article: write to news_items, post to news-headlines thread,
    and optionally embed for semantic recall (Phase B).

    Returns (news_written, thread_posted, embedded). Each True iff that sink
    actually wrote (vs deduped/skipped/no-op)."""
    primary = (h.get("symbol") or fallback_symbol or "").upper() or None
    tickers = [(t or "").upper() for t in (h.get("tickers") or []) if t]
    if primary and primary not in tickers:
        tickers = [primary] + tickers

    # Resolve agent tags. Union the per-ticker sector_map+watchlist tags; if
    # we have tickers but none of them are in any agent's universe, fall back
    # to macro keyword routing on the headline.
    agent_tags: set[str] = set()
    for t in tickers:
        agent_tags.update(symbol_to_agents.get(t, set()))
    if not agent_tags:
        agent_tags = macro_tags_for(h.get("headline") or "")
    agent_tag_list = sorted(agent_tags)

    category = categorize(h.get("channels"), h.get("headline") or "")
    importance = importance_for(category)
    sentiment = _normalize_sentiment(h.get("sentiment"))
    article_id = h.get("article_id") or None

    # ── Sink 1: news_items ───────────────────────────────────────────────────
    try:
        await store.write_news(
            symbol=primary,
            headline=h.get("headline") or "",
            article_id=article_id,
            provider=h.get("provider") or None,
            url=h.get("url"),
            body=h.get("body"),
            sentiment=sentiment,
            channels=h.get("channels") or None,
            published_at=h.get("time") or None,
            category=category,
            importance=importance,
            agent_tags=agent_tag_list or None,
        )
        news_written = True
    except Exception:
        log.exception("write_news failed for symbol=%s article=%s", primary, article_id)
        news_written = False

    # ── Sink 2: news-headlines thread ────────────────────────────────────────
    # Dedup by article_id in post.meta (GIN-indexed lookup).
    thread_posted = False
    skip_thread = False
    if article_id:
        try:
            existing = await store.find_post_by_article_id(article_id)
        except Exception:
            existing = None
        if existing:
            skip_thread = True

    body = (h.get("body") or h.get("headline") or "").strip()
    if not body:
        # post.body is required-non-empty. Skip thread sink when nothing to write.
        skip_thread = True

    if not skip_thread:
        try:
            await store.post_to_thread(
                thread_slug="news-headlines",
                author=f"feed:{(h.get('provider') or 'massive').lower().replace(' ', '_')[:32]}",
                author_kind="external_feed",
                title=(h.get("headline") or "")[:200] or "(untitled)",
                body=body[:8000],
                meta={
                    "agents": agent_tag_list,
                    "category": category,
                    "importance": importance,
                    "symbol": primary,
                    "tickers": tickers,
                    "sentiment": sentiment,
                    "article_id": article_id,
                    "url": h.get("url"),
                    "published_at": h.get("time"),
                    "provider": h.get("provider"),
                },
            )
            thread_posted = True
        except Exception:
            log.exception("post_to_thread failed for article=%s", article_id)

    # ── Sink 3: embedding (Phase B, best-effort) ─────────────────────────────
    # Skip when no provider configured, no article_id (can't UPDATE the row),
    # or pgvector columns aren't there yet. set_news_embedding swallows the
    # column-missing case so this is safe to call unconditionally.
    embedded = False
    if news_written and article_id and embeddings.provider_ready():
        text = embeddings.text_for_embedding(h.get("headline") or "", h.get("body") or "")
        if text:
            try:
                vec = await embeddings.embed_one(text)
            except Exception:
                log.exception("embed_one failed for article=%s", article_id)
                vec = None
            if vec is not None:
                model = embeddings.active_model_name() or "unknown"
                embedded = await store.set_news_embedding(article_id, vec, model)
    return news_written, thread_posted, embedded


async def ingest_one(symbol: str, max_items: int, symbol_to_agents: dict[str, set[str]]) -> dict:
    """Fetch + dual-write for one symbol."""
    try:
        payload = await massive_client.get_news(symbol, max_items=max_items)
    except Exception as exc:
        log.warning("get_news(%s) failed: %s: %s", symbol, type(exc).__name__, exc)
        return {"fetched": 0, "news": 0, "thread": 0, "embedded": 0}
    headlines = payload.get("headlines") or []
    n_news = n_thread = n_embed = 0
    for h in headlines:
        nw, tp, em = await _write_article(h, symbol_to_agents, fallback_symbol=symbol)
        if nw:
            n_news += 1
        if tp:
            n_thread += 1
        if em:
            n_embed += 1
    return {"fetched": len(headlines), "news": n_news, "thread": n_thread, "embedded": n_embed}


async def ingest_market(max_items: int, symbol_to_agents: dict[str, set[str]]) -> dict:
    """Single no-ticker pull — catches macro headlines that don't show up under
    any single symbol (FOMC, jobs, OPEC, broad indices)."""
    try:
        payload = await massive_client.get_market_news(max_items=max_items)
    except Exception as exc:
        log.warning("get_market_news failed: %s: %s", type(exc).__name__, exc)
        return {"fetched": 0, "news": 0, "thread": 0, "embedded": 0}
    headlines = payload.get("headlines") or []
    n_news = n_thread = n_embed = 0
    for h in headlines:
        nw, tp, em = await _write_article(h, symbol_to_agents, fallback_symbol=None)
        if nw:
            n_news += 1
        if tp:
            n_thread += 1
        if em:
            n_embed += 1
    return {"fetched": len(headlines), "news": n_news, "thread": n_thread, "embedded": n_embed}


# ── Top-level driver ──────────────────────────────────────────────────────────

def _is_rth_now() -> bool:
    """Return True iff now is within 04:00–20:00 ET (covers pre-market + RTH + after-hours)."""
    try:
        from zoneinfo import ZoneInfo
        et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        et = datetime.now(timezone.utc)
    return 4 <= et.hour < 20


async def run(
    symbols: list[str],
    max_items: int,
    symbol_to_agents: dict[str, set[str]],
    skip_market_pull: bool,
    market_max: int,
) -> None:
    if not symbols and skip_market_pull:
        log.info("nothing to fetch (no symbols, market pull disabled) — exiting")
        return
    if symbols:
        log.info("ingesting %d symbols (max_items=%d)", len(symbols), max_items)
    total = {"fetched": 0, "news": 0, "thread": 0, "embedded": 0}
    sem = asyncio.Semaphore(4)

    async def _bounded(s: str) -> dict:
        async with sem:
            return await ingest_one(s, max_items, symbol_to_agents)

    results = await asyncio.gather(*(_bounded(s) for s in symbols), return_exceptions=False)
    for r in results:
        for k in total:
            total[k] += r.get(k, 0)

    if not skip_market_pull:
        log.info("market pull (no-ticker) max_items=%d", market_max)
        m = await ingest_market(market_max, symbol_to_agents)
        for k in total:
            total[k] += m.get(k, 0)
        log.info(
            "  market: fetched=%d news=%d thread=%d embedded=%d",
            m["fetched"], m["news"], m["thread"], m["embedded"],
        )

    log.info(
        "done — %d symbols, %d fetched, %d news_items, %d thread posts, %d embedded "
        "(provider=%s)",
        len(symbols), total["fetched"], total["news"], total["thread"],
        total["embedded"], embeddings.active_model_name() or "none",
    )
    await massive_client.aclose()
    await embeddings.aclose()


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--symbols", help="Comma-separated tickers (overrides automatic universe)")
    p.add_argument("--max-items", type=int, default=20, help="Per-symbol max headlines (default 20)")
    p.add_argument("--market-max", type=int, default=40, help="Items pulled in the no-ticker market sweep (default 40)")
    p.add_argument("--skip-market-pull", action="store_true", help="Skip the no-ticker market-news sweep")
    p.add_argument("--rth-only", action="store_true", help="Exit early if outside 04:00–20:00 ET")
    p.add_argument("--once", action="store_true", help="Run once and exit (default; kept for symmetry)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.rth_only and not _is_rth_now():
        log.info("outside RTH (04:00–20:00 ET) — exiting early per --rth-only")
        return 0

    if not os.environ.get("MASSIVE_API_KEY", "").strip():
        log.error("MASSIVE_API_KEY missing — cannot fetch news")
        return 2

    async def _entry() -> None:
        universe, symbol_to_agents = await collect_target_tickers()
        if args.symbols:
            symbols = sorted({s.strip().upper() for s in args.symbols.split(",") if s.strip()})
        else:
            symbols = sorted(universe)
        await run(
            symbols=symbols,
            max_items=args.max_items,
            symbol_to_agents=symbol_to_agents,
            skip_market_pull=args.skip_market_pull,
            market_max=args.market_max,
        )

    asyncio.run(_entry())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
