"""Pre-trade risk orchestrator. Runs all checks in order; first failure blocks."""

from __future__ import annotations

import inspect
import logging

from risk.models import AccountState, OrderRequest, RiskResult, ALLOWED
from risk.checks import (
    allocation,
    kill_switch,
    market_hours,
    mode_lock,
    order_size,
    position_size,
    daily_loss,
)

log = logging.getLogger(__name__)

# Ordered list — first failure wins
_CHECKS = [
    allocation,     # block zero-allocation agents (mike, cassidy, disabled) before anything else
    kill_switch,
    market_hours,
    mode_lock,
    order_size,
    position_size,
    daily_loss,
]


async def check(order: OrderRequest, account: AccountState, cfg: dict) -> RiskResult:
    for checker in _CHECKS:
        result = checker.check(order, account, cfg)
        if inspect.isawaitable(result):
            result = await result
        if not result.allowed:
            log.warning(
                "Risk block [%s]: %s | agent=%s symbol=%s qty=%s",
                result.check_name, result.reason, order.agent_name, order.symbol, order.quantity,
            )
            return result
    return ALLOWED
