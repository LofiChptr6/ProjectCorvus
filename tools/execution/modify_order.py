TOOL_DEF = {
    "name": "modify_order",
    "description": (
        "Modify the price or quantity of an open limit order. "
        "Can only modify orders that are still working (not filled). "
        "Modified orders go through risk checks again."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "order_id": {"type": "integer", "description": "IBKR order ID to modify."},
            "new_limit_price": {"type": "number", "description": "New limit price. Omit to keep current."},
            "new_quantity": {"type": "number", "description": "New quantity."},
            "reasoning": {"type": "string", "description": "Why you are modifying this order."},
        },
        "required": ["order_id", "reasoning"],
    },
}


async def execute(order_id: int, reasoning: str, new_limit_price: float = None, new_quantity: float = None, **_) -> str:
    import json
    from ibkr.orders import modify_order
    result = await modify_order(order_id, new_limit_price, new_quantity)
    return json.dumps(result)
