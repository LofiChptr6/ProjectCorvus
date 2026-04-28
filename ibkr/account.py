"""Account state: positions, balances, open orders."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def get_positions() -> list[dict]:
    from ibkr.client import get_ib

    ib = await get_ib()
    await ib.reqPositionsAsync()
    positions = []
    for p in ib.positions():
        contract = p.contract
        # IBKR's Position object carries marketPrice/marketValue/unrealizedPNL
        # alongside avgCost. Surface them so every caller sees mark-to-market
        # unrealized P&L without a second round-trip.
        market_price = float(getattr(p, "marketPrice", 0.0) or 0.0)
        market_value = float(getattr(p, "marketValue", 0.0) or 0.0)
        unrealized_pnl = float(getattr(p, "unrealizedPNL", 0.0) or 0.0)
        positions.append({
            "symbol": contract.symbol,
            "quantity": p.position,
            "avg_cost": p.avgCost,
            "market_price": market_price,
            "market_value": market_value,
            "unrealized_pnl": unrealized_pnl,
        })
    return positions


async def get_account_summary() -> dict:
    from ibkr.client import get_ib, get_mode

    ib = await get_ib()
    await ib.reqAccountSummaryAsync()
    account_values = ib.accountValues()

    result: dict = {"mode": get_mode()}
    tags_wanted = {"NetLiquidation", "TotalCashValue", "BuyingPower", "RealizedPnL", "UnrealizedPnL"}
    for item in account_values:
        if item.tag not in tags_wanted or item.currency != "USD":
            continue
        if item.tag == "NetLiquidation":
            result["nav"] = float(item.value)
        elif item.tag == "TotalCashValue":
            result["cash"] = float(item.value)
        elif item.tag == "BuyingPower":
            result["buying_power"] = float(item.value)
        elif item.tag == "RealizedPnL":
            result["realized_pnl_today"] = float(item.value)
        elif item.tag == "UnrealizedPnL":
            result["unrealized_pnl"] = float(item.value)

    return result


async def get_open_orders() -> list[dict]:
    from ibkr.client import get_ib

    ib = await get_ib()
    trades = await ib.reqOpenOrdersAsync()
    orders = []
    for trade in trades:
        o = trade.order
        s = trade.orderStatus
        orders.append({
            "order_id": o.orderId,
            "symbol": trade.contract.symbol,
            "action": o.action,
            "order_type": o.orderType,
            "quantity": o.totalQuantity,
            "filled": s.filled,
            "remaining": s.remaining,
            "limit_price": o.lmtPrice if o.lmtPrice and o.lmtPrice > 0 else None,
            "stop_price": o.auxPrice if o.auxPrice and o.auxPrice > 0 else None,
            "status": s.status,
            "avg_fill_price": s.avgFillPrice if s.avgFillPrice > 0 else None,
        })
    return orders
