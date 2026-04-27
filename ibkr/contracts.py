"""Resolve ticker symbols to IBKR Contract objects."""

from __future__ import annotations

import logging
from functools import lru_cache

log = logging.getLogger(__name__)

_contract_cache: dict[str, object] = {}


async def resolve(symbol: str, currency: str = "USD", exchange: str = "SMART"):
    """Return a qualified IBKR Stock contract for the given symbol."""
    cache_key = f"{symbol}:{currency}:{exchange}"
    if cache_key in _contract_cache:
        return _contract_cache[cache_key]

    from ib_async import Stock
    from ibkr.client import get_ib

    ib = await get_ib()
    contract = Stock(symbol, exchange, currency)
    details = await ib.reqContractDetailsAsync(contract)
    if not details:
        raise ValueError(f"No contract found for symbol '{symbol}'")

    qualified = details[0].contract
    _contract_cache[cache_key] = qualified
    log.debug("Resolved %s → conId=%s", symbol, qualified.conId)
    return qualified
