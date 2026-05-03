"""Resolve ticker symbols to qualified contracts via the IBKR daemon.

The daemon also caches contracts; this per-process LRU saves a daemon
roundtrip on hot paths (e.g. quote loops within a single skill run). The
returned dict has the same `symbol`, `conId`, `exchange`, `primary_exchange`,
`currency` shape the daemon emits — callers don't get a real `ib_async.Contract`
anymore (they shouldn't need one — placing an order is a daemon RPC).
"""

from __future__ import annotations

import logging
from typing import Any

from ibkr import _rpc

log = logging.getLogger(__name__)

_contract_cache: dict[str, dict] = {}


async def resolve(symbol: str, currency: str = "USD",
                  exchange: str = "SMART") -> dict:
    cache_key = f"{symbol}:{currency}:{exchange}"
    if cache_key in _contract_cache:
        return _contract_cache[cache_key]

    qualified = await _rpc.post("/resolve", {
        "symbol": symbol,
        "currency": currency,
        "exchange": exchange,
    })
    _contract_cache[cache_key] = qualified
    log.debug("Resolved %s → conId=%s", symbol, qualified.get("conId"))
    return qualified
