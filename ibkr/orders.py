"""Order placement, modification, cancellation, and fill tracking."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import db.store as store

log = logging.getLogger(__name__)

# Maps our DB order ID → IBKR trade object for fill monitoring
_active_trades: dict[int, object] = {}


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
    from ib_async import LimitOrder, MarketOrder, StopOrder
    from ibkr.client import get_ib, get_mode
    from ibkr.contracts import resolve

    ib = await get_ib()
    contract = await resolve(symbol)
    mode = get_mode()

    if order_type == "MKT":
        ibkr_order = MarketOrder(action, quantity)
    elif order_type == "LMT":
        if limit_price is None:
            raise ValueError("limit_price required for LMT orders")
        ibkr_order = LimitOrder(action, quantity, limit_price)
    elif order_type == "STP":
        if stop_price is None:
            raise ValueError("stop_price required for STP orders")
        ibkr_order = StopOrder(action, quantity, stop_price)
    else:
        raise ValueError(f"Unknown order_type: {order_type}")

    trade = ib.placeOrder(contract, ibkr_order)
    await asyncio.sleep(0.5)  # Let IB assign orderId

    ibkr_order_id = ibkr_order.orderId

    # Write to DB
    order_id = await store.write_order(
        session_id=session_id,
        agent_name=agent_name,
        symbol=symbol,
        action=action,
        order_type=order_type,
        quantity=quantity,
        limit_price=limit_price,
        stop_price=stop_price,
        status="submitted",
        risk_approved=True,
        human_approved=None,
        rejection_reason=None,
        reasoning=reasoning,
        mode=mode,
        ibkr_order_id=ibkr_order_id,
    )

    # Register fill callback
    _active_trades[order_id] = trade
    trade.fillEvent += lambda trade, fill, report: asyncio.create_task(
        _on_fill(order_id, agent_name, mode, fill, report)
    )

    # Replay fills that landed during the sleep+DB-write window (market orders
    # routinely fill in <500ms, before the handler above is attached).
    # write_fill is idempotent via ON CONFLICT (ibkr_exec_id) DO NOTHING.
    for existing_fill in list(trade.fills):
        report = getattr(existing_fill, "commissionReport", None)
        await _on_fill(order_id, agent_name, mode, existing_fill, report)

    log.info("Placed %s %s %s x%s → ibkr_id=%s", action, order_type, symbol, quantity, ibkr_order_id)
    return {
        "status": "submitted",
        "order_id": order_id,
        "ibkr_order_id": ibkr_order_id,
        "symbol": symbol,
        "action": action,
        "quantity": quantity,
        "order_type": order_type,
        "limit_price": limit_price,
        "stop_price": stop_price,
        "mode": mode,
    }


async def _on_fill(
    order_id: int, agent_name: str, mode: str, fill, report
) -> None:
    try:
        # IBKR's commissionReport carries realizedPNL when the fill closes (or
        # partially closes) a position. The unset sentinel is sys.float_info.max;
        # treat anything implausibly large as missing.
        realized_pnl: Optional[float] = None
        if report is not None:
            rp = getattr(report, "realizedPNL", None)
            if rp is not None and abs(float(rp)) < 1e100:
                realized_pnl = float(rp)

        symbol = fill.contract.symbol
        await store.write_fill(
            ibkr_exec_id=fill.execution.execId,
            order_id=order_id,
            agent_name=agent_name,
            filled_at=datetime.now(timezone.utc).isoformat(),
            symbol=symbol,
            action=fill.execution.side,
            quantity=fill.execution.shares,
            fill_price=fill.execution.price,
            commission=report.commission if report else None,
            exchange=fill.execution.exchange,
            mode=mode,
            realized_pnl=realized_pnl,
        )
        await store.update_order_status(order_id, "filled")

        # Settle attribution. Closing fills carry realizedPNL but the
        # closing decision has no attribution rows for the symbol (the
        # SELL trim is not driven by a contributing_view). Realized P&L
        # belongs on the OPENING decision's rows. Re-walk the symbol's
        # fills via FIFO; this is idempotent and self-heals across
        # partial fills, missing realizedPNL, and out-of-order callbacks.
        try:
            from meta_agent.pnl_attribution import reconcile_symbol
            res = await reconcile_symbol(symbol)
            if res["rows_updated"] > 0:
                log.info(
                    "Attributed P&L on %s: %d rows updated (events=%d, already_set=%d)",
                    symbol, res["rows_updated"], res["events"], res["skipped_already_set"],
                )
        except Exception as exc:
            log.warning("reconcile_symbol(%s) failed: %s", symbol, exc)

        log.info("Fill recorded: order_id=%s exec_id=%s", order_id, fill.execution.execId)
    except Exception as exc:
        log.error("Fill callback error: %s", exc)


async def cancel_order(ibkr_order_id: int) -> dict:
    from ibkr.client import get_ib

    ib = await get_ib()
    open_trades = await ib.reqOpenOrdersAsync()
    for trade in open_trades:
        if trade.order.orderId == ibkr_order_id:
            ib.cancelOrder(trade.order)
            log.info("Cancelled order ibkr_id=%s", ibkr_order_id)
            return {"status": "cancelled", "ibkr_order_id": ibkr_order_id}
    return {"status": "not_found", "ibkr_order_id": ibkr_order_id}


async def modify_order(
    ibkr_order_id: int,
    new_limit_price: Optional[float] = None,
    new_quantity: Optional[float] = None,
) -> dict:
    from ibkr.client import get_ib
    from ibkr.contracts import resolve

    ib = await get_ib()
    open_trades = await ib.reqOpenOrdersAsync()
    for trade in open_trades:
        if trade.order.orderId == ibkr_order_id:
            order = trade.order
            if new_limit_price is not None:
                order.lmtPrice = new_limit_price
            if new_quantity is not None:
                order.totalQuantity = new_quantity
            ib.placeOrder(trade.contract, order)
            return {"status": "modified", "ibkr_order_id": ibkr_order_id}
    return {"status": "not_found", "ibkr_order_id": ibkr_order_id}
