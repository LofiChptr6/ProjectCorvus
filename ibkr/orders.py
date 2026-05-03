"""Order placement, modification, cancellation.

Thin client over the IBKR daemon (see ibkr/daemon.py). The daemon owns the
fillEvent callbacks, _active_trades map, and DB writes — those used to live
here, but moving them server-side ensures every fill is recorded exactly once
regardless of which caller initiated the order.
"""

from __future__ import annotations

import logging
from typing import Optional

from ibkr import _rpc

log = logging.getLogger(__name__)


async def place_order(
    symbol: str,
    action: str,
    quantity: float,
    order_type: str,
    limit_price: Optional[float] = None,
    stop_price: Optional[float] = None,
    agent_name: str = "",
    session_id: Optional[str] = None,
    reasoning: Optional[str] = None,
) -> dict:
    return await _rpc.post("/place_order", {
        "symbol": symbol,
        "action": action,
        "quantity": quantity,
        "order_type": order_type,
        "limit_price": limit_price,
        "stop_price": stop_price,
        "agent_name": agent_name,
        "session_id": session_id,
        "reasoning": reasoning,
    })


async def cancel_order(ibkr_order_id: int) -> dict:
    return await _rpc.post("/cancel_order", {"ibkr_order_id": ibkr_order_id})


async def modify_order(
    ibkr_order_id: int,
    new_limit_price: Optional[float] = None,
    new_quantity: Optional[float] = None,
) -> dict:
    return await _rpc.post("/modify_order", {
        "ibkr_order_id": ibkr_order_id,
        "new_limit_price": new_limit_price,
        "new_quantity": new_quantity,
    })
