"""Read/write agent capital allocations as % of live NAV.

The single source of truth is `allocation_pct` (0.0–1.0). Dollar amounts are
always derived as `pct × current_NAV` at read time, so NAV changes propagate
automatically without manual rebalancing.

Disabled agents (or unallocated %) become idle cash — other agents do NOT
auto-grow to fill the gap (option B). To fully deploy capital, propose an
explicit allocation change.
"""

from __future__ import annotations

import db.store as store
from agent.agent_registry import list_agents


async def _live_nav() -> float:
    """Fetch current account NAV from IBKR. Returns 0 on failure."""
    try:
        from ibkr.account import get_account_summary
        s = await get_account_summary()
        return float(s.get("nav", 0) or 0)
    except Exception:
        return 0.0


async def get_all_allocations(nav: float | None = None) -> list[dict]:
    """Return per-agent allocation: pct (DB or YAML default) + derived USD at current NAV."""
    if nav is None:
        nav = await _live_nav()

    db_rows = {r["agent_name"]: r for r in await store.get_allocations()}
    agents = list_agents(enabled_only=False)
    result = []
    for a in agents:
        name = a["name"]
        db_entry = db_rows.get(name)
        if db_entry is not None:
            pct = float(db_entry["allocation_pct"])
            source = "db"
            updated_at = db_entry.get("updated_at")
        else:
            pct = float(a.get("allocation_pct", 0))
            source = "yaml_default"
            updated_at = None
        result.append({
            "agent_name": name,
            "allocation_pct": pct,
            "allocated_usd": pct * nav,
            "updated_at": updated_at,
            "source": source,
            "enabled": a.get("enabled", True),
        })
    return result


async def set_allocation(agent_name: str, pct: float, by: str = "cli") -> None:
    """Set agent's NAV percentage. Validates 0.0–1.0 and warns if total enabled sum > 1.0."""
    await store.set_allocation(agent_name, pct, updated_by=by)
    # Soft check: warn if total now exceeds 100%. Don't block — rebalances are multi-step.
    rows = await get_all_allocations(nav=1.0)  # nav doesn't matter for sum check
    total = sum(r["allocation_pct"] for r in rows if r["enabled"])
    if total > 1.0:
        import logging
        logging.getLogger(__name__).warning(
            "Allocation sum exceeds 100%%: %.1f%% — over-deployed by %.1f%%",
            total * 100, (total - 1.0) * 100,
        )


async def get_effective_allocation_pct(agent_name: str) -> float:
    """Return the effective allocation % for an agent (DB first, then YAML default)."""
    rows = await store.get_allocations()
    for r in rows:
        if r["agent_name"] == agent_name:
            return float(r["allocation_pct"])
    from agent.agent_registry import load_agent
    cfg = load_agent(agent_name)
    return float(cfg.get("allocation_pct", 0))


async def get_effective_allocation(agent_name: str, nav: float | None = None) -> float:
    """Return the effective allocation in USD: pct × current NAV."""
    pct = await get_effective_allocation_pct(agent_name)
    if nav is None:
        nav = await _live_nav()
    return pct * nav
