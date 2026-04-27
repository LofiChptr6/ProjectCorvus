TOOL_DEF = {
    "name": "get_balances",
    "description": (
        "Get account balances: net asset value (NAV), available cash, "
        "buying power, and today's realized P&L. "
        "Use this to understand how much capital is available and daily performance."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}


async def execute(**_) -> str:
    import json
    from ibkr.account import get_account_summary
    result = await get_account_summary()
    return json.dumps(result)
