"""massive.com market-data client.

Drop-in replacement for the market-data half of `ibkr/market_data.py`.
Mirrors the exact return shapes so downstream consumers (compute_technicals,
tools/market/*, mcp_server, concierge/tools) need only swap the import.

Endpoints used (all under https://api.massive.com):
  - GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}      → get_quote
  - GET /v2/aggs/ticker/{ticker}/range/{mult}/{span}/{from}/{to}    → get_bars
  - GET /v2/reference/news?ticker={t}&limit={n}                     → get_news

Auth: Bearer token from MASSIVE_API_KEY env var.

Note: `what_to_show` (TRADES/MIDPOINT/BID/ASK) is silently accepted but
ignored — massive returns trade aggregates only. Same for `useRTH`.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.massive.com"
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        api_key = os.environ.get("MASSIVE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "MASSIVE_API_KEY not set. Add it to .env or export it before "
                "calling massive_client.* functions."
            )
        _client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
        )
    return _client


async def _get_json(path: str, params: Optional[dict] = None) -> dict:
    """GET with two retries on 429 / 5xx."""
    import asyncio
    client = _get_client()
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            r = await client.get(path, params=params)
            if r.status_code in (429, 500, 502, 503, 504):
                last_exc = httpx.HTTPStatusError(
                    f"massive returned {r.status_code}: {r.text[:200]}",
                    request=r.request, response=r,
                )
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_exc = e
            await asyncio.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"massive.com request failed after 3 attempts: {path} ({last_exc})")


# ── get_quote ────────────────────────────────────────────────────────────────

async def get_quote(symbol: str) -> dict:
    """Return shape matches ibkr.market_data.get_quote — same keys, same types."""
    sym = symbol.upper()
    data = await _get_json(f"/v2/snapshot/locale/us/markets/stocks/tickers/{sym}")
    t = data.get("ticker") or {}
    last_quote = t.get("lastQuote") or {}
    last_trade = t.get("lastTrade") or {}
    day = t.get("day") or {}
    prev = t.get("prevDay") or {}

    bid = last_quote.get("p")
    ask = last_quote.get("P")
    last = last_trade.get("p")
    # Fall back to day close (intraday) or prev close (pre-market) if no print yet
    close = day.get("c") or prev.get("c")
    if not last:
        last = close

    return {
        "symbol": sym,
        "bid": bid,
        "ask": ask,
        "last": last,
        "close": close,
        "volume": day.get("v") or prev.get("v"),
        "day_high": day.get("h"),
        "day_low": day.get("l"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── get_bars ─────────────────────────────────────────────────────────────────

_BAR_SIZE_MAP: dict[str, tuple[int, str]] = {
    "1 secs": (1, "second"),
    "5 secs": (5, "second"),
    "15 secs": (15, "second"),
    "30 secs": (30, "second"),
    "1 min": (1, "minute"),
    "2 mins": (2, "minute"),
    "5 mins": (5, "minute"),
    "15 mins": (15, "minute"),
    "30 mins": (30, "minute"),
    "1 hour": (1, "hour"),
    "1 day": (1, "day"),
    "1 week": (1, "week"),
}

_DURATION_UNIT_DAYS = {"S": 1, "D": 1, "W": 7, "M": 30, "Y": 365}


def _duration_to_days(duration: str) -> int:
    parts = duration.strip().upper().split()
    if not parts:
        return 1
    try:
        n = int(parts[0])
    except ValueError:
        return 1
    unit = parts[1] if len(parts) > 1 else "D"
    return max(1, n * _DURATION_UNIT_DAYS.get(unit, 1))


async def get_bars(
    symbol: str,
    bar_size: str = "5 mins",
    duration: str = "1 D",
    what_to_show: str = "TRADES",
) -> dict:
    """Return shape: {symbol, bar_size, duration, bars: [{t,o,h,l,c,v}]}.

    `what_to_show` is accepted for signature parity but ignored — massive
    returns trade aggregates only.
    """
    sym = symbol.upper()
    if bar_size not in _BAR_SIZE_MAP:
        raise ValueError(f"Unsupported bar_size: {bar_size!r}")
    multiplier, timespan = _BAR_SIZE_MAP[bar_size]

    days_back = _duration_to_days(duration)
    today = datetime.now(timezone.utc).date()
    # Pad lookback so weekends/holidays don't shrink the window below caller's intent
    from_date = (today - timedelta(days=days_back + 7)).isoformat()
    to_date = today.isoformat()

    data = await _get_json(
        f"/v2/aggs/ticker/{sym}/range/{multiplier}/{timespan}/{from_date}/{to_date}",
        params={"adjusted": "true", "sort": "asc", "limit": 50000},
    )
    results = data.get("results") or []
    bars = []
    for b in results:
        ts_ms = b.get("t")
        iso = (
            datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
            if ts_ms is not None else ""
        )
        bars.append({
            "t": iso,
            "o": b.get("o"),
            "h": b.get("h"),
            "l": b.get("l"),
            "c": b.get("c"),
            "v": b.get("v"),
        })
    return {
        "symbol": sym,
        "bar_size": bar_size,
        "duration": duration,
        "bars": bars,
    }


# ── get_news ─────────────────────────────────────────────────────────────────

async def get_news(symbol: Optional[str] = None, max_items: int = 10) -> dict:
    """Return shape: {symbol, headlines: [{time, symbol, headline, provider, article_id, ...}]}.

    Core fields (always present) match the original Polygon-shape contract so older
    callers don't break. With the Benzinga add-on enabled, additional fields are
    populated when present in the response: `body`, `url`, `sentiment` (-1..1 if
    Massive scores it, else None), `channels` (list of Benzinga channel slugs).
    Absent fields are emitted as None / [] so JSON shape stays predictable.
    """
    if not symbol:
        return {"symbol": symbol, "headlines": []}
    sym = symbol.upper()
    params = {
        "ticker": sym,
        "limit": max(1, min(max_items, 1000)),
        "order": "descending",
        "sort": "published_utc",
    }
    data = await _get_json("/v2/reference/news", params=params)
    results = data.get("results") or []
    headlines = []
    for n in results:
        publisher = n.get("publisher") or {}
        # Sentiment may live under several keys depending on Massive's flattening
        # of Benzinga insights. Try the obvious ones; surface None if not present.
        sentiment = n.get("sentiment")
        if sentiment is None:
            insights = n.get("insights") or []
            if isinstance(insights, list) and insights:
                # Take the entry whose `ticker` matches our symbol if any.
                match = next((i for i in insights if (i or {}).get("ticker") == sym), insights[0])
                sentiment = (match or {}).get("sentiment")
        headlines.append({
            "time": n.get("published_utc", ""),
            "symbol": sym,
            "headline": n.get("title", ""),
            "provider": publisher.get("name", ""),
            "article_id": n.get("id", ""),
            # Benzinga-enriched fields (None / [] when absent)
            "url": n.get("article_url") or n.get("url"),
            "body": n.get("description") or n.get("body"),
            "sentiment": sentiment,
            "channels": n.get("keywords") or n.get("channels") or [],
        })
    return {"symbol": sym, "headlines": headlines}


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
