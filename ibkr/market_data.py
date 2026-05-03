"""IBKR market data — kept only for `run_scanner` in practice.

`get_quote`, `get_bars`, and `get_news` are deprecated for live callers (which
use `data.massive_client` instead) but the functions remain here as RPCs for
any legacy reference. Only `run_scanner` is on the live IBKR path.
"""

from __future__ import annotations

import logging
from typing import Optional

from ibkr import _rpc

log = logging.getLogger(__name__)


async def get_quote(symbol: str) -> dict:
    return await _rpc.post("/quote", {"symbol": symbol})


async def get_bars(
    symbol: str,
    bar_size: str = "5 mins",
    duration: str = "1 D",
    what_to_show: str = "TRADES",
) -> dict:
    return await _rpc.post("/bars", {
        "symbol": symbol,
        "bar_size": bar_size,
        "duration": duration,
        "what_to_show": what_to_show,
    })


async def run_scanner(
    scan_type: str,
    num_rows: int = 20,
    above_price: Optional[float] = None,
    below_price: Optional[float] = None,
    above_volume: Optional[int] = None,
) -> dict:
    return await _rpc.post("/scanner", {
        "scan_type": scan_type,
        "num_rows": num_rows,
        "above_price": above_price,
        "below_price": below_price,
        "above_volume": above_volume,
    })


async def get_news(symbol: Optional[str] = None, max_items: int = 10) -> dict:
    return await _rpc.post("/news", {
        "symbol": symbol,
        "max_items": max_items,
    })
