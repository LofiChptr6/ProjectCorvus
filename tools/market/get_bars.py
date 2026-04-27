TOOL_DEF = {
    "name": "get_bars",
    "description": (
        "Get historical OHLCV price bars for a symbol. "
        "Use for trend analysis, support/resistance, and context before trading decisions. "
        "Returns bars in chronological order, newest last."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Stock ticker symbol."},
            "bar_size": {
                "type": "string",
                "enum": ["1 min", "5 mins", "15 mins", "1 hour", "1 day"],
                "description": "Bar interval.",
            },
            "duration": {
                "type": "string",
                "description": "How far back to fetch, e.g. '1 D', '5 D', '1 M', '3 M'.",
            },
            "what_to_show": {
                "type": "string",
                "enum": ["TRADES", "MIDPOINT", "BID", "ASK"],
                "description": "Data type. Default: TRADES.",
            },
        },
        "required": ["symbol", "bar_size", "duration"],
    },
}


async def execute(symbol: str, bar_size: str, duration: str, what_to_show: str = "TRADES", **_) -> str:
    import json
    from data.massive_client import get_bars
    result = await get_bars(symbol, bar_size, duration, what_to_show)
    return json.dumps(result)
