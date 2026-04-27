"""Pure-Python technical indicator computation on IBKR bar data."""

TOOL_DEF = {
    "name": "compute_technicals",
    "description": (
        "Compute technical indicators on price data for a symbol. "
        "Fetches recent bars and calculates the requested indicators. "
        "Use to add quantitative signal to trading decisions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Stock ticker symbol."},
            "indicators": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["SMA_20", "SMA_50", "SMA_200", "EMA_9", "EMA_21",
                             "RSI_14", "VWAP", "ATR_14", "BBANDS_20"],
                },
                "description": "List of indicators to compute.",
            },
        },
        "required": ["symbol", "indicators"],
    },
}


def _sma(closes: list[float], n: int):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _ema(closes: list[float], n: int):
    if len(closes) < n:
        return None
    k = 2 / (n + 1)
    ema = sum(closes[:n]) / n
    for c in closes[n:]:
        ema = c * k + ema * (1 - k)
    return ema


def _rsi(closes: list[float], n: int = 14):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-n:]) / n
    avg_loss = sum(losses[-n:]) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def _vwap(bars: list[dict]):
    cum_vol = cum_tpv = 0.0
    for b in bars:
        typical = (b["h"] + b["l"] + b["c"]) / 3
        cum_tpv += typical * b["v"]
        cum_vol += b["v"]
    return cum_tpv / cum_vol if cum_vol else None


def _atr(bars: list[dict], n: int = 14):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        hl = bars[i]["h"] - bars[i]["l"]
        hc = abs(bars[i]["h"] - bars[i - 1]["c"])
        lc = abs(bars[i]["l"] - bars[i - 1]["c"])
        trs.append(max(hl, hc, lc))
    return sum(trs[-n:]) / n


def _bbands(closes: list[float], n: int = 20):
    if len(closes) < n:
        return None, None, None
    window = closes[-n:]
    mid = sum(window) / n
    std = (sum((x - mid) ** 2 for x in window) / n) ** 0.5
    return round(mid + 2 * std, 4), round(mid, 4), round(mid - 2 * std, 4)


async def execute(symbol: str, indicators: list, **_) -> str:
    import json
    from data.massive_client import get_bars

    # Fetch enough bars for all indicators (200 days for SMA_200)
    bar_data = await get_bars(symbol, "1 day", "1 Y")
    bars = bar_data.get("bars", [])
    closes = [b["c"] for b in bars]
    last_price = closes[-1] if closes else None

    result: dict = {"symbol": symbol, "price": last_price, "indicators": {}}

    for ind in indicators:
        if ind == "SMA_20":
            result["indicators"]["SMA_20"] = _sma(closes, 20)
        elif ind == "SMA_50":
            result["indicators"]["SMA_50"] = _sma(closes, 50)
        elif ind == "SMA_200":
            result["indicators"]["SMA_200"] = _sma(closes, 200)
        elif ind == "EMA_9":
            result["indicators"]["EMA_9"] = _ema(closes, 9)
        elif ind == "EMA_21":
            result["indicators"]["EMA_21"] = _ema(closes, 21)
        elif ind == "RSI_14":
            result["indicators"]["RSI_14"] = _rsi(closes, 14)
        elif ind == "VWAP":
            result["indicators"]["VWAP"] = _vwap(bars)
        elif ind == "ATR_14":
            result["indicators"]["ATR_14"] = _atr(bars, 14)
        elif ind == "BBANDS_20":
            upper, mid, lower = _bbands(closes, 20)
            result["indicators"]["BBANDS_20"] = {"upper": upper, "mid": mid, "lower": lower}

    for k, v in result["indicators"].items():
        if isinstance(v, float):
            result["indicators"][k] = round(v, 4)

    return json.dumps(result)
