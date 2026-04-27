import logging

from risk.models import OrderRequest, AccountState, RiskResult, ALLOWED
import db.store as store

log = logging.getLogger(__name__)


async def check(order: OrderRequest, account: AccountState, cfg: dict) -> RiskResult:
    # Fail closed on DB error — if we can't confirm the kill switch is OFF, treat
    # it as ON. A stale read here cannot wrongly authorize an order.
    try:
        killed = await store.is_killed(agent_name=order.agent_name)
    except Exception as exc:
        log.error("kill_switch check: DB error, denying order: %s", exc)
        return RiskResult(
            allowed=False,
            reason=f"Kill switch state unknown (DB error: {exc}); denying.",
            check_name="kill_switch",
        )
    if killed:
        return RiskResult(
            allowed=False,
            reason="Kill switch is active — all trading halted.",
            check_name="kill_switch",
        )
    return ALLOWED
