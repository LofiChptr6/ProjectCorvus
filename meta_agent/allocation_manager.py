"""Agent roster helper.

In the post-2026-04-26 conviction-driven architecture there is **no per-agent
capital allocation**. Mike-allocator sizes the whole desk against
consolidated conviction views; sector agents publish convictions and never
hold a NAV sleeve. This module's sole remaining job is to enumerate the
agent roster (with the legacy allocation columns left in the response shape
at 0.0 so callers like Cassidy's Step 4b.0 audit don't break).

The `agent_allocations` DB table is retained but no longer written.
`set_allocation` was removed 2026-05-12 along with the CLI `allocate`
command — submit a `submit_conviction_view` instead.
"""

from __future__ import annotations

import db.store as store
from agent.agent_registry import list_agents


async def get_all_allocations(nav: float | None = None) -> list[dict]:
    """Return the agent roster with always-zero allocation fields.

    Kept for back-compat with `get_agent_list` (MCP tool) and Cassidy's
    Step 4b.0 audit, which iterates the list and checks per-agent state.
    The `allocation_pct` / `allocated_usd` fields are vestigial and always
    0 under the conviction-driven architecture."""
    db_rows = {r["agent_name"]: r for r in await store.get_allocations()}
    return [
        {
            "agent_name": a["name"],
            "allocation_pct": float(db_rows[a["name"]]["allocation_pct"]) if a["name"] in db_rows else 0.0,
            "allocated_usd": 0.0,
            "updated_at": db_rows.get(a["name"], {}).get("updated_at"),
            "source": "db" if a["name"] in db_rows else "missing",
            "enabled": a.get("enabled", True),
            "direct_trade_allowed": bool(a.get("direct_trade_allowed", False)),
        }
        for a in list_agents(enabled_only=False)
    ]
