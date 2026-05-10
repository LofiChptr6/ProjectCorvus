"""In-process tool dispatch.

Each entry in AGENT_TOOL_REGISTRY maps an OpenAI-style function name to an
async handler that takes a kwarg dict and returns a JSON-encoded string. The
handlers wrap the *same* Python functions that mcp_server.py exposes — we just
skip the MCP transport.

Schemas (used by tool_loop to declare the toolset to vLLM) are kept here as
Anthropic-shape `{name, description, input_schema}` dicts, then translated to
OpenAI function-tool shape at call time. This matches the pattern in
concierge/tools.py and the translator in concierge/chat.py:60-75.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

JsonStr = str
ToolHandler = Callable[[dict[str, Any]], Awaitable[JsonStr]]

# ── Handlers ──────────────────────────────────────────────────────────────────
# Keep them tiny + lazy-import their backing modules so importing this file is
# cheap even when most of the codebase isn't loaded yet.


async def _tool_get_quote(args: dict[str, Any]) -> JsonStr:
    from data.massive_client import get_quote
    return json.dumps(await get_quote(args["symbol"]), default=str)


async def _tool_compute_technicals(args: dict[str, Any]) -> JsonStr:
    from tools.analysis.compute_technicals import execute as _ct
    indicators = args.get("indicators") or [
        "SMA_20", "SMA_50", "RSI_14", "ATR_14", "BBANDS_20",
    ]
    # `execute` already returns a JSON string per its mcp_server usage.
    return await _ct(symbol=args["symbol"], indicators=indicators)


async def _tool_get_bars(args: dict[str, Any]) -> JsonStr:
    from data.massive_client import get_bars
    return json.dumps(await get_bars(
        args["symbol"], args.get("bar_size", "1 day"),
        args.get("duration", "30 D"), args.get("what_to_show", "TRADES"),
    ), default=str)


async def _tool_get_news(args: dict[str, Any]) -> JsonStr:
    from db import store
    rows = await store.get_recent_news(
        symbol=args.get("symbol"), limit=int(args.get("limit", 20)),
    )
    return json.dumps(rows, default=str)


async def _tool_mark_inbox_responded(args: dict[str, Any]) -> JsonStr:
    from db import store
    updated = await store.mark_inbox_responded(
        inbox_id=int(args["inbox_id"]),
        response_body=args["response_body"],
        agent_name=args["agent_name"],
        response_session_id=args.get("response_session_id"),
    )
    return json.dumps({"updated": updated})


async def _tool_get_my_active_views(args: dict[str, Any]) -> JsonStr:
    from db import store
    rows = await store.get_agent_active_convictions(args["agent_name"])
    return json.dumps({"views": rows}, default=str)


async def _tool_get_my_journal(args: dict[str, Any]) -> JsonStr:
    from datetime import date as _date
    from db import store
    name = args["agent_name"]
    today = _date.today().isoformat()
    open_t = await store.get_open_theses(name, limit=10)
    due = await store.get_theses_due(name, on_or_before=today)
    resolved = await store.get_recent_resolutions(name, limit=3)
    return json.dumps(
        {"open": open_t, "due_today_or_earlier": due, "recent_resolutions": resolved},
        default=str,
    )


async def _tool_compute_all_models(args: dict[str, Any]) -> JsonStr:
    from data.massive_client import get_bars as _get_bars
    from meta_agent.model_loader import run_all_models
    try:
        from ibkr.account import get_account_summary
        summary = await get_account_summary()
    except Exception:
        summary = {}
    bars_resp = await _get_bars(
        args["symbol"], args.get("bar_size", "1 day"),
        args.get("duration", "1 Y"), "TRADES",
    )
    bars = bars_resp.get("bars", []) if isinstance(bars_resp, dict) else bars_resp
    result = await run_all_models(
        agent_name=args["agent_name"], symbol=args["symbol"],
        bars=bars, account_summary=summary,
    )
    return json.dumps(result, default=str)


# ── Registry ──────────────────────────────────────────────────────────────────

AGENT_TOOL_REGISTRY: dict[str, ToolHandler] = {
    "get_quote": _tool_get_quote,
    "compute_technicals": _tool_compute_technicals,
    "get_bars": _tool_get_bars,
    "get_news": _tool_get_news,
    "mark_inbox_responded": _tool_mark_inbox_responded,
    "get_my_active_views": _tool_get_my_active_views,
    "get_my_journal": _tool_get_my_journal,
    "compute_all_models": _tool_compute_all_models,
}


# Anthropic-shape schemas. tool_loop.to_openai_tools() translates these.
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "get_quote": {
        "name": "get_quote",
        "description": "Live quote for a single symbol (last, bid, ask, volume, day change).",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    "compute_technicals": {
        "name": "compute_technicals",
        "description": "Compute named technical indicators on a symbol's recent bars. Default set: SMA_20/SMA_50/RSI_14/ATR_14/BBANDS_20.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "indicators": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["symbol"],
        },
    },
    "get_bars": {
        "name": "get_bars",
        "description": "Historical OHLCV bars.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "bar_size": {"type": "string"},
                "duration": {"type": "string"},
                "what_to_show": {"type": "string"},
            },
            "required": ["symbol"],
        },
    },
    "get_news": {
        "name": "get_news",
        "description": "Recent news headlines, optionally filtered by symbol.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
    "mark_inbox_responded": {
        "name": "mark_inbox_responded",
        "description": "Mark a pending dashboard question as responded with your reply.",
        "input_schema": {
            "type": "object",
            "properties": {
                "inbox_id": {"type": "integer"},
                "response_body": {"type": "string"},
                "agent_name": {"type": "string"},
            },
            "required": ["inbox_id", "response_body", "agent_name"],
        },
    },
    "get_my_active_views": {
        "name": "get_my_active_views",
        "description": "Read your currently active conviction views.",
        "input_schema": {
            "type": "object",
            "properties": {"agent_name": {"type": "string"}},
            "required": ["agent_name"],
        },
    },
    "get_my_journal": {
        "name": "get_my_journal",
        "description": "Read your journal: open theses, predictions due today or earlier, recent resolutions.",
        "input_schema": {
            "type": "object",
            "properties": {"agent_name": {"type": "string"}},
            "required": ["agent_name"],
        },
    },
    "compute_all_models": {
        "name": "compute_all_models",
        "description": "Run every quant model in agents/<agent_name>/models/ on a symbol; returns per-model output + error_count + flat_count.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_name": {"type": "string"},
                "symbol": {"type": "string"},
                "bar_size": {"type": "string"},
                "duration": {"type": "string"},
            },
            "required": ["agent_name", "symbol"],
        },
    },
}


async def dispatch(name: str, args: dict[str, Any]) -> JsonStr:
    """Run a tool by name. Unknown name → JSON {"error": ...}. Exceptions
    captured into the same envelope so the LLM sees a result and can recover."""
    handler = AGENT_TOOL_REGISTRY.get(name)
    if handler is None:
        return json.dumps({"error": f"unknown tool: {name}"})
    try:
        return await handler(args or {})
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


def filter_schemas(allowed: set[str] | None) -> list[dict[str, Any]]:
    """Return TOOL_SCHEMAS values restricted to `allowed`. None = all."""
    if allowed is None:
        return list(TOOL_SCHEMAS.values())
    return [TOOL_SCHEMAS[n] for n in allowed if n in TOOL_SCHEMAS]
