"""Account state: positions, balances, open orders.

Thin client over the IBKR daemon (see ibkr/daemon.py).
"""

from __future__ import annotations

import logging

from ibkr import _rpc

log = logging.getLogger(__name__)


async def get_positions() -> list[dict]:
    return await _rpc.get("/positions")


async def get_account_summary() -> dict:
    return await _rpc.get("/balances")


async def get_open_orders() -> list[dict]:
    return await _rpc.get("/open_orders")
