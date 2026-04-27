TOOL_DEF = {
    "name": "get_open_orders",
    "description": (
        "Get all currently open (working) orders that have not yet been filled or cancelled. "
        "Use after placing an order to confirm submission, or to check for partial fills."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}


async def execute(**_) -> str:
    import json
    from ibkr.account import get_open_orders
    orders = await get_open_orders()
    return json.dumps({"orders": orders})
