TOOL_DEF = {
    "name": "run_scanner",
    "description": (
        "Run an IBKR market scanner to find stocks matching specific criteria. "
        "Good for morning scans and finding trading opportunities."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "scan_type": {
                "type": "string",
                "enum": [
                    "TOP_PERC_GAIN", "TOP_PERC_LOSE",
                    "MOST_ACTIVE", "HOT_BY_VOLUME",
                    "TOP_PRICE_RANGE", "HIGH_VS_13W_HL", "LOW_VS_13W_HL",
                ],
                "description": "Scanner type.",
            },
            "num_rows": {"type": "integer", "description": "Max results (default 20).", "default": 20},
            "above_price": {"type": "number", "description": "Filter: only stocks above this price."},
            "below_price": {"type": "number", "description": "Filter: only stocks below this price."},
            "above_volume": {"type": "integer", "description": "Filter: minimum volume."},
        },
        "required": ["scan_type"],
    },
}


async def execute(
    scan_type: str,
    num_rows: int = 20,
    above_price: float = None,
    below_price: float = None,
    above_volume: int = None,
    **_,
) -> str:
    import json
    from ibkr.market_data import run_scanner
    result = await run_scanner(scan_type, num_rows, above_price, below_price, above_volume)
    return json.dumps(result)
