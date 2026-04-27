import asyncio
import logging

from risk.models import OrderRequest, AccountState, RiskResult, ALLOWED
import db.store as store

log = logging.getLogger(__name__)


async def check(order: OrderRequest, account: AccountState, cfg: dict) -> RiskResult:
    max_loss = cfg.get("risk", {}).get("max_daily_loss", 500)
    realized = account.realized_pnl_today

    if realized < -abs(max_loss):
        # Auto-activate global kill switch
        await store.set_kill_switch(
            active=True,
            agent_name=None,
            activated_by="daily_loss_circuit",
            reason=f"Daily realized P&L ${realized:,.2f} exceeded limit -${abs(max_loss):,.0f}",
        )
        log.critical("CIRCUIT BREAKER: daily loss $%.2f exceeds limit $%.0f", realized, max_loss)
        return RiskResult(
            allowed=False,
            reason=f"Daily loss circuit breaker triggered. Realized P&L: ${realized:,.2f} (limit: -${max_loss:,.0f}). Kill switch activated.",
            check_name="daily_loss",
        )
    return ALLOWED
