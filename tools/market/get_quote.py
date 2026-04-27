TOOL_DEF = {
    "name": "get_quote",
    "description": (
        "Get the current real-time quote for a stock symbol. "
        "Returns bid, ask, last price, volume, and day change. "
        "Call this before placing any order to check current price."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Stock ticker symbol, e.g. 'AAPL'."}
        },
        "required": ["symbol"],
    },
}


async def execute(symbol: str, **_) -> str:
    import json
    from data.massive_client import get_quote
    result = await get_quote(symbol)
    return json.dumps(result)
