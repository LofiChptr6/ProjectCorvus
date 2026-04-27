TOOL_DEF = {
    "name": "get_pnl_summary",
    "description": (
        "Get P&L summary for today or a specific date range. "
        "Returns realized P&L (from closed trades), unrealized P&L (open positions), and trade count."
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
    totals = {"realized_pnl": 0.0, "unrealized_pnl": 0.0, "total_pnl": 0.0, "num_fills": 0}
    for r in rows:
        totals["realized_pnl"] += r.get("realized_pnl", 0) or 0
        totals["unrealized_pnl"] += r.get("unrealized_pnl", 0) or 0
        totals["total_pnl"] += r.get("total_pnl", 0) or 0
        totals["num_fills"] += r.get("num_fills", 0) or 0

    return json.dumps({"period": period, "by_agent": rows, "totals": totals})
