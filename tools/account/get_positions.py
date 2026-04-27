TOOL_DEF = {
    "name": "get_positions",
    "description": (
        "Get all current open positions with quantity, average cost, "
        "current market value, and unrealized P&L. "
        "Always check this before deciding to buy or sell to avoid unwanted exposure."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}


async def execute(**_) -> str:
    import json
    from ibkr.account import get_positions
    positions = await get_positions()
    return json.dumps({"positions": positions})
