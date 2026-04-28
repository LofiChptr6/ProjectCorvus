import math

from risk.models import OrderRequest, AccountState, RiskResult, ALLOWED


def _agent_overrides(agent_name: str) -> dict:
    if not agent_name:
        return {}
    try:
        from agent.agent_registry import load_agent
        return load_agent(agent_name).get("risk_overrides", {}) or {}
    except Exception:
        return {}


def evaluate_min_quantity(quantity: float, price: float | None, cfg: dict) -> tuple[str, str]:
    """Stateless helper used by both the standard guardrail check and the
    allocator pre-filter in rebalance_desk. Returns (status, reason) where
    status ∈ {"ok", "reject", "needs_telegram_approval"}."""
    risk = cfg.get("risk", {})
    min_shares = float(risk.get("min_shares_per_order", 10) or 10)
    expensive = float(risk.get("expensive_share_threshold_usd", 300.0) or 300.0)
    if quantity >= min_shares:
        return "ok", ""
    if price is not None and price >= expensive:
        return (
            "needs_telegram_approval",
            f"sub-{int(min_shares)}-share order on expensive ticker (price ${price:.2f}/sh)",
        )
    return (
        "reject",
        f"min {int(min_shares)} shares per ticker — pick a cheaper underlying "
        f"(e.g. leveraged ETF) or scale up",
    )


def check(order: OrderRequest, account: AccountState, cfg: dict) -> RiskResult:
    risk = cfg.get("risk", {})
    overrides = _agent_overrides(order.agent_name)
    # Tighter of the two wins (agent override can only restrict, not expand, the global cap).
    global_max_value = risk.get("max_order_value", 10_000)
    global_max_shares = risk.get("max_order_shares", 1_000)
    agent_max_value = overrides.get("max_order_value")
    max_value = min(global_max_value, agent_max_value) if agent_max_value is not None else global_max_value
    max_shares = global_max_shares

    # Reject zero/negative/NaN/Inf qty before any arithmetic — IBKR would reject
    # but cleaner to fail-closed here so audit trail and risk metrics are honest.
    if not isinstance(order.quantity, (int, float)) or not math.isfinite(order.quantity) or order.quantity <= 0:
        return RiskResult(
            allowed=False,
            reason=f"Order quantity must be a positive finite number, got {order.quantity!r}.",
            check_name="order_size",
        )

    if order.quantity > max_shares:
        return RiskResult(
            allowed=False,
            reason=f"Order quantity {order.quantity} exceeds max {max_shares} shares.",
            check_name="order_size",
        )

    # Estimate notional from limit_price or use a conservative NAV-based estimate
    price = order.effective_price
    if price and price > 0:
        notional = order.quantity * price
        if notional > max_value:
            return RiskResult(
                allowed=False,
                reason=f"Order notional ${notional:,.0f} exceeds limit ${max_value:,.0f}.",
                check_name="order_size",
            )

    # Sub-10-share gate. Requires a price (limit/stop or current_mark). When the
    # caller couldn't supply a price, we let it through here; the place_order
    # tool fetches a quote and re-checks before submission.
    status, reason = evaluate_min_quantity(order.quantity, price, cfg)
    if status == "reject":
        return RiskResult(allowed=False, reason=reason, check_name="min_quantity")
    if status == "needs_telegram_approval":
        return RiskResult(
            allowed=False,
            reason=reason,
            check_name="min_quantity",
            needs_telegram_approval=True,
        )

    return ALLOWED
