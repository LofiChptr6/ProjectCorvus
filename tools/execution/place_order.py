"""The most consequential tool: place_order integrates risk, approval, and IBKR execution."""

from __future__ import annotations

from typing import Optional

TOOL_DEF = {
    "name": "place_order",
    "description": (
        "Place a stock order. Supports market (MKT), limit (LMT), and stop (STP) orders. "
        "Orders go through risk checks before submission — if a check fails, "
        "the reason is returned and no order is placed. "
        "Large orders may require human Telegram approval. "
        "Always call get_quote first to verify current price. "
        "IMPORTANT: double-check symbol spelling. "
        "The 'reasoning' field is REQUIRED and logged for every trade."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Stock ticker symbol."},
            "action": {"type": "string", "enum": ["BUY", "SELL"], "description": "Trade direction."},
            "quantity": {"type": "number", "description": "Number of shares. Must be positive."},
            "order_type": {
                "type": "string",
                "enum": ["MKT", "LMT", "STP"],
                "description": "MKT=market, LMT=limit, STP=stop.",
            },
            "limit_price": {
                "type": "number",
                "description": "Required for LMT orders. Max buy / min sell price.",
            },
            "stop_price": {"type": "number", "description": "Required for STP orders. Trigger price."},
            "reasoning": {
                "type": "string",
                "description": (
                    "REQUIRED. Why are you placing this order? Be specific: "
                    "what signal, entry criteria, stop level, and target drove this decision."
                ),
            },
        },
        "required": ["symbol", "action", "quantity", "order_type", "reasoning"],
    },
}

_context: dict = {}


def set_context(agent_name: str, session_id: str, cfg: dict) -> None:
    _context.update({"agent_name": agent_name, "session_id": session_id, "cfg": cfg})


async def execute(
    symbol: str,
    action: str,
    quantity: float,
    order_type: str,
    reasoning: str,
    limit_price: Optional[float] = None,
    stop_price: Optional[float] = None,
    **_,
) -> str:
    import json
    import db.store as store
    from ibkr.account import get_account_summary, get_positions
    from risk.models import AccountState, OrderRequest
    from risk.guardrails import check as risk_check

    agent_name = _context.get("agent_name", "unknown")
    session_id = _context.get("session_id")
    cfg = _context.get("cfg", {})

    # If in dry-run mode, skip everything
    if _context.get("dry_run"):
        return json.dumps({
            "status": "dry_run",
            "would_place": {
                "symbol": symbol, "action": action, "quantity": quantity,
                "order_type": order_type, "limit_price": limit_price,
                "stop_price": stop_price, "reasoning": reasoning,
            },
        })

    # Build order + account state for risk checks
    order = OrderRequest(
        symbol=symbol, action=action, quantity=quantity, order_type=order_type,
        limit_price=limit_price, stop_price=stop_price, reasoning=reasoning,
        agent_name=agent_name, session_id=session_id,
    )
    summary = await get_account_summary()
    positions = await get_positions()
    account = AccountState(
        nav=summary.get("nav", 0),
        cash=summary.get("cash", 0),
        buying_power=summary.get("buying_power", 0),
        realized_pnl_today=summary.get("realized_pnl_today", 0),
        positions=positions,
    )

    risk_result = await risk_check(order, account, cfg)
    if not risk_result.allowed:
        # Log risk-rejected orders so the audit trail covers what we *tried* to
        # do, not just what got through. Best-effort — never break the response
        # for a logging failure.
        try:
            await store.write_order(
                session_id=session_id, agent_name=agent_name, symbol=symbol,
                action=action, order_type=order_type, quantity=quantity,
                limit_price=limit_price, stop_price=stop_price,
                status="risk_rejected", risk_approved=False,
                human_approved=None, rejection_reason=risk_result.reason,
                reasoning=reasoning, mode=cfg.get("trading", {}).get("mode", "paper"),
            )
        except Exception:
            pass
        return json.dumps({"status": "blocked", "reason": risk_result.reason, "check": risk_result.check_name})

    # Human approval check
    price = limit_price or stop_price or 0
    notional = quantity * price
    approval_cfg = cfg.get("approval", {})
    if approval_cfg.get("enabled", True) and notional >= approval_cfg.get("threshold_usd", 5000):
        from approval.workflow import request_approval
        approval = await request_approval(order, notional, session_id, cfg)
        if not approval.approved:
            await store.write_order(
                session_id=session_id, agent_name=agent_name, symbol=symbol,
                action=action, order_type=order_type, quantity=quantity,
                limit_price=limit_price, stop_price=stop_price,
                status="approval_rejected", risk_approved=True,
                human_approved=False, rejection_reason=approval.reason,
                reasoning=reasoning, mode=cfg.get("trading", {}).get("mode", "paper"),
            )
            return json.dumps({"status": "approval_rejected", "reason": approval.reason})

    # Re-check kill switch immediately before IBKR submit. The approval workflow
    # can pause for `approval.timeout_s` seconds; during that window the user
    # could trip the kill switch in Telegram. If we can't reach the DB, deny —
    # a stale read here can't wrongly authorize.
    try:
        if await store.is_killed(agent_name=agent_name):
            await store.write_order(
                session_id=session_id, agent_name=agent_name, symbol=symbol,
                action=action, order_type=order_type, quantity=quantity,
                limit_price=limit_price, stop_price=stop_price,
                status="kill_switch_blocked", risk_approved=True,
                human_approved=True, rejection_reason="kill switch tripped during approval window",
                reasoning=reasoning, mode=cfg.get("trading", {}).get("mode", "paper"),
            )
            return json.dumps({"status": "blocked", "reason": "kill switch tripped during approval window", "check": "kill_switch"})
    except Exception as exc:
        return json.dumps({"status": "blocked", "reason": f"kill switch state unknown (DB error: {exc}); denying", "check": "kill_switch"})

    # Submit to IBKR
    from ibkr.orders import place_order as ibkr_place
    result = await ibkr_place(
        symbol=symbol, action=action, quantity=quantity, order_type=order_type,
        limit_price=limit_price, stop_price=stop_price,
        agent_name=agent_name, session_id=session_id, reasoning=reasoning,
    )
    return json.dumps(result)
