TOOL_DEF = {
    "name": "cancel_order",
    "description": (
        "Cancel an open order by its IBKR order ID. "
        "Use get_open_orders to find the order ID. "
        "Returns success or failure with reason."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "order_id": {"type": "integer", "description": "IBKR order ID from get_open_orders."},
            "reasoning": {"type": "string", "description": "Why you are cancelling this order."},
        },
        "required": ["order_id", "reasoning"],
    },
}


async def execute(order_id: int, reasoning: str, **_) -> str:
    import json
    from ibkr.orders import cancel_order
    result = await cancel_order(order_id)
    return json.dumps(result)
