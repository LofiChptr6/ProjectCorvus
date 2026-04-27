TOOL_DEF = {
    "name": "get_trade_blotter",
    "description": (
        "Get a list of executed fills (trade history). "
        "Useful for reviewing what trades were made and at what prices."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Filter by symbol. Omit for all."},
            "date": {"type": "string", "description": "Filter by date YYYY-MM-DD. Omit for today."},
            "limit": {"type": "integer", "description": "Max fills (default 50).", "default": 50},
        },
        "required": [],
    },
}


async def execute(symbol: str = None, date: str = None, limit: int = 50, **_) -> str:
    import json
    from datetime import date as dt_date
    import db.store as store

    if date is None:
        date = dt_date.today().isoformat()

    fills = await store.get_fills(symbol=symbol, date=date, limit=limit)
    return json.dumps({"fills": fills, "count": len(fills)})
