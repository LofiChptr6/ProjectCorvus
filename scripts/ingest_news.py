#!/usr/bin/env python
"""Periodic news ingestor — Massive (Benzinga add-on) → Postgres news_items.

Run by systemd timer trading-news-ingest.timer every 15 min during RTH. Walks
every agent's watchlist.md "Active" section, dedups tickers, pulls Massive
news for each, and INSERTs into news_items. Conflicts on article_id are
swallowed so reruns are cheap.

Manual:
    python scripts/ingest_news.py --once
    python scripts/ingest_news.py --symbols AAPL,SPY,VIX --max-items 20
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

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

from data import massive_client
from db import store

log = logging.getLogger("ingest_news")

_TICKER_RE = re.compile(r"^\s*-\s+([A-Z][A-Z0-9.\-]{0,9})\s+—")
_ACTIVE_HEADER_RE = re.compile(r"^\s*##\s+Active\s*$", re.IGNORECASE)
_ANY_H2_RE = re.compile(r"^\s*##\s+")


def _read_active_tickers(path: Path) -> set[str]:
    """Parse the '## Active' section of an agent watchlist.md, return tickers."""
    if not path.exists():
        return set()
    tickers: set[str] = set()
    in_active = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if _ACTIVE_HEADER_RE.match(line):
            in_active = True
            continue
        if in_active and _ANY_H2_RE.match(line):
            break
        if in_active:
            m = _TICKER_RE.match(line)
            if m:
                tickers.add(m.group(1).upper())
    return tickers


def collect_watchlist_tickers() -> set[str]:
    """Union of every agent's Active watchlist."""
    out: set[str] = set()
    for wl in (ROOT / "agents").glob("*/watchlist.md"):
        out |= _read_active_tickers(wl)
    return out


async def ingest_one(symbol: str, max_items: int) -> tuple[int, int]:
    """Fetch + insert news for one symbol. Returns (fetched, written-or-deduped)."""
    try:
        payload = await massive_client.get_news(symbol, max_items=max_items)
    except Exception as exc:
        log.warning("get_news(%s) failed: %s: %s", symbol, type(exc).__name__, exc)
        return (0, 0)
    headlines = payload.get("headlines") or []
    written = 0
    for h in headlines:
        try:
            await store.write_news(
                symbol=h.get("symbol") or symbol,
                headline=h.get("headline") or "",
                article_id=h.get("article_id") or None,
                provider=h.get("provider") or None,
                url=h.get("url"),
                body=h.get("body"),
                sentiment=h.get("sentiment"),
                channels=h.get("channels") or None,
                published_at=h.get("time") or None,
            )
            written += 1
        except Exception:
            log.exception("write_news failed for %s", symbol)
    return (len(headlines), written)


async def run(symbols: list[str], max_items: int) -> None:
    if not symbols:
        log.info("no symbols to ingest (empty watchlists?) — exiting")
        return
    log.info("ingesting %d symbols: %s", len(symbols), ", ".join(sorted(symbols)))
    total_fetched = 0
    total_written = 0
    # Modest concurrency — Massive is not infinitely fast and we want to avoid 429s.
    sem = asyncio.Semaphore(4)

    async def _bounded(s: str) -> tuple[str, int, int]:
        async with sem:
            f, w = await ingest_one(s, max_items)
            return (s, f, w)

    results = await asyncio.gather(*(_bounded(s) for s in symbols))
    for s, f, w in results:
        total_fetched += f
        total_written += w
        if f:
            log.info("  %s: fetched=%d written/deduped=%d", s, f, w)
    log.info("done — %d symbols, %d fetched, %d written", len(symbols), total_fetched, total_written)
    await massive_client.aclose()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--symbols", help="Comma-separated tickers (overrides watchlist scan)")
    p.add_argument("--max-items", type=int, default=20, help="Per-symbol max headlines (default 20)")
    p.add_argument("--once", action="store_true", help="Run once and exit (default behavior; kept for symmetry)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.symbols:
        symbols = sorted({s.strip().upper() for s in args.symbols.split(",") if s.strip()})
    else:
        symbols = sorted(collect_watchlist_tickers())

    if not os.environ.get("MASSIVE_API_KEY", "").strip():
        log.error("MASSIVE_API_KEY missing — cannot fetch news")
        return 2

    asyncio.run(run(symbols, args.max_items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
