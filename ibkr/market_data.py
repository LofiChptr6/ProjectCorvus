"""IBKR market data — kept ONLY for `run_scanner`.

`get_quote`, `get_bars`, and `get_news` here are deprecated; live callers now
use `data.massive_client` (see plan: market data swapped to massive.com to
avoid IBKR market-data subscription requirement and the reqAccountSummary
subscription leak that hit during quote loops). The functions remain in this
file as a fallback reference but are no longer imported by tools/, mcp_server,
or concierge.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


async def get_quote(symbol: str) -> dict:
    from ib_async import util
    from ibkr.client import get_ib
    from ibkr.contracts import resolve

    ib = await get_ib()
    contract = await resolve(symbol)
    ticker = ib.reqMktData(contract, "", False, False)
    await asyncio.sleep(2)  # Allow snapshot to arrive
    ib.cancelMktData(contract)

    def _val(v):
        return None if v != v or v == 1.7976931348623157e+308 else v  # nan / max float

    return {
        "symbol": symbol,
        "bid": _val(ticker.bid),
        "ask": _val(ticker.ask),
        "last": _val(ticker.last),
        "close": _val(ticker.close),
        "volume": _val(ticker.volume),
        "day_high": _val(ticker.high),
        "day_low": _val(ticker.low),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def get_bars(
    symbol: str,
    bar_size: str = "5 mins",
    duration: str = "1 D",
    what_to_show: str = "TRADES",
) -> dict:
    from ibkr.client import get_ib
    from ibkr.contracts import resolve

    ib = await get_ib()
    contract = await resolve(symbol)
    bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting=bar_size,
        whatToShow=what_to_show,
        useRTH=True,
        formatDate=1,
    )
    return {
        "symbol": symbol,
        "bar_size": bar_size,
        "duration": duration,
        "bars": [
            {
                "t": b.date.isoformat() if hasattr(b.date, "isoformat") else str(b.date),
                "o": b.open,
                "h": b.high,
                "l": b.low,
                "c": b.close,
                "v": b.volume,
            }
            for b in bars
        ],
    }


async def run_scanner(
    scan_type: str,
    num_rows: int = 20,
    above_price: Optional[float] = None,
    below_price: Optional[float] = None,
    above_volume: Optional[int] = None,
) -> dict:
    from ib_async import ScannerSubscription
    from ibkr.client import get_ib

    ib = await get_ib()
    sub = ScannerSubscription(
        instrument="STK",
        locationCode="STK.US.MAJOR",
        scanCode=scan_type,
        numberOfRows=num_rows,
    )
    if above_price is not None:
        sub.abovePrice = above_price
    if below_price is not None:
        sub.belowPrice = below_price
    if above_volume is not None:
        sub.aboveVolume = above_volume

    scan_data = await ib.reqScannerDataAsync(sub)
    results = []
    for i, item in enumerate(scan_data):
        c = item.contractDetails.contract
        results.append({
            "rank": i + 1,
            "symbol": c.symbol,
            "exchange": getattr(c, "primaryExchange", None) or c.exchange,
        })
    return {"scan_type": scan_type, "results": results}


async def get_news(symbol: Optional[str] = None, max_items: int = 10) -> dict:
    from ibkr.client import get_ib
    from ibkr.contracts import resolve

    ib = await get_ib()

    headlines = []
    if symbol:
        contract = await resolve(symbol)
        news_list = await ib.reqHistoricalNewsAsync(
            contract.conId,
            providerCodes="BRFG+DJNL",
            startDateTime="",
            endDateTime="",
            totalResults=max_items,
        )
        for n in news_list:
            headlines.append({
                "time": str(n.time),
                "symbol": symbol,
                "headline": n.headline,
                "provider": n.providerCode,
                "article_id": n.articleId,
            })
    return {"symbol": symbol, "headlines": headlines}
