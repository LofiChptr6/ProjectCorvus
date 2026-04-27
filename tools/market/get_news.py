TOOL_DEF = {
    "name": "get_news",
    "description": (
        "Fetch recent news headlines for a stock symbol or the general market. "
        "Use to understand catalysts before making trading decisions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Stock ticker. Omit for market news."},
            "max_items": {"type": "integer", "description": "Max headlines (default 10).", "default": 10},
        },
        "required": [],
    },
}


async def execute(symbol: str = None, max_items: int = 10, **_) -> str:
    import json
    from data.massive_client import get_news
    result = await get_news(symbol, max_items)
    return json.dumps(result)
