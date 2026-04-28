TOOL_DEF = {
    "name": "get_pnl_summary",
    "description": (
        "Get attributed P&L summary for today or a specific date range. "
        "Returns each agent's slice of Mike's realized P&L (apportioned by their "
        "share of the conviction stack on each closed trade) and fill count."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "period": {
                "type": "string",
                "enum": ["today", "week", "month", "all"],
                "description": "Time period for the summary.",
                "default": "today",
            }
        },
        "required": [],
    },
}


async def execute(period: str = "today", **_) -> str:
    import json
    import db.store as store

    rows = await store.get_pnl_summary(period=period)
    totals = {"total_pnl": 0.0, "num_fills": 0}
    for r in rows:
        totals["total_pnl"] += r.get("total_pnl", 0) or 0
        totals["num_fills"] += r.get("num_fills", 0) or 0

    return json.dumps({"period": period, "by_agent": rows, "totals": totals})
