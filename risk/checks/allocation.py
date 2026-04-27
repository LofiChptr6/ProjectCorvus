"""Blocks orders from agents whose allocation_pct is zero (Cassidy, disabled agents).

In the sector-shard architecture (post-2026-04-26), `mike` is the desk's *allocator*
and trades on behalf of the entire NAV — not a per-agent sleeve. Sector agents
publish convictions and never place orders directly. So this check exempts mike
(its YAML allocation_pct=0 reflects "director, not its own sleeve" — not "barred
from trading"). Mike's trades are still gated by kill_switch, market_hours,
order_size, position_size, and the Telegram approval threshold.
"""

from risk.models import OrderRequest, AccountState, RiskResult, ALLOWED


async def check(order: OrderRequest, account: AccountState, cfg: dict) -> RiskResult:
    if not order.agent_name:
        return ALLOWED

    # Allocator bypass — mike trades the whole desk, not a sleeve.
    if order.agent_name == "mike":
        return ALLOWED

    try:
        from agent.agent_registry import load_agent
        load_agent(order.agent_name)
    except FileNotFoundError:
        return RiskResult(
            allowed=False,
            reason=f"Unknown agent '{order.agent_name}' — no YAML found.",
            check_name="allocation",
        )

    from agent.agent_registry import load_agent as _load
    agent_cfg = _load(order.agent_name)
    if not agent_cfg.get("enabled", True):
        return RiskResult(
            allowed=False,
            reason=f"Agent '{order.agent_name}' is disabled.",
            check_name="allocation",
        )

    # Effective % comes from DB (Mike's overrides) or YAML default.
    from meta_agent.allocation_manager import get_effective_allocation_pct
    pct = await get_effective_allocation_pct(order.agent_name)
    if pct <= 0:
        return RiskResult(
            allowed=False,
            reason=f"Agent '{order.agent_name}' has zero allocation — not a trading agent.",
            check_name="allocation",
        )
    return ALLOWED
