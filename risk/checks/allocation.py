"""Blocks orders from agents that aren't authorized to trade directly.

In the sector-shard architecture (post-2026-04-26), only `mike` (the allocator)
places real orders. Sector agents publish convictions and never trade directly.
Authorization is a per-agent boolean flag in `agents/<name>.yaml`:
`direct_trade_allowed: true`. Default is false. Disabled agents are also blocked.

The legacy `allocation_pct` field has been retired — capital allocation is no
longer expressed as a per-agent percentage. Mike sizes the whole desk from
consolidated convictions.
"""

from risk.models import OrderRequest, AccountState, RiskResult, ALLOWED


async def check(order: OrderRequest, account: AccountState, cfg: dict) -> RiskResult:
    if not order.agent_name:
        return ALLOWED

    try:
        from agent.agent_registry import load_agent
        agent_cfg = load_agent(order.agent_name)
    except FileNotFoundError:
        return RiskResult(
            allowed=False,
            reason=f"Unknown agent '{order.agent_name}' — no YAML found.",
            check_name="allocation",
        )

    if not agent_cfg.get("enabled", True):
        return RiskResult(
            allowed=False,
            reason=f"Agent '{order.agent_name}' is disabled.",
            check_name="allocation",
        )

    if not agent_cfg.get("direct_trade_allowed", False):
        return RiskResult(
            allowed=False,
            reason=(
                f"Agent '{order.agent_name}' is conviction-only — not authorized "
                "to place orders directly. Only the allocator (mike) trades."
            ),
            check_name="allocation",
        )
    return ALLOWED
