"""Tool definitions for the Sonnet concierge.

The toolset is intentionally narrow: mostly read-only, plus two WRITE tools
(resolve_proposal, propose_strategic_change) that both go through an explicit
user-confirmation gate before actually executing. Execution-level trading
(place_order, cancel_order, activate_kill_switch) is NEVER exposed to Sonnet.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


# ── Anthropic tool schema ─────────────────────────────────────────────────────

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_positions",
        "description": "List all open positions across the desk. Returns symbol, qty, avg cost, market value, unrealized P&L. Read-only.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_balances",
        "description": "Account summary: NAV, cash, buying power, realized P&L today, unrealized P&L. Read-only.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_pnl_summary",
        "description": "Per-agent P&L for a given period. Use this when the user asks 'how's Rex doing' or 'what's the desk P&L today'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["today", "week", "month"], "default": "today"},
                "agent_name": {"type": "string", "description": "Optional: filter to one agent (rex/maya/atlas/titan/vera)."},
            },
        },
    },
    {
        "name": "get_open_orders",
        "description": "Working (unfilled) orders across the desk. Read-only.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_quote",
        "description": "Live quote for a single symbol (last, bid, ask, volume, day change). Use for price checks.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "Ticker, e.g. SPY, AAPL."}},
            "required": ["symbol"],
        },
    },
    {
        "name": "get_agent_list",
        "description": "List configured agents (rex/maya/atlas/titan/vera/mike/cassidy) with allocation and enabled status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "enabled_only": {"type": "boolean", "default": False, "description": "If true, only return enabled agents."},
            },
        },
    },
    {
        "name": "get_mike_analysis",
        "description": "Fetch Mike (the director)'s daily market analysis. Can scope to a specific agent's guidance section.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "default": "today", "description": "YYYY-MM-DD or 'today'."},
                "agent_name": {"type": "string", "description": "Optional: return only this agent's guidance plus regime + risk_tone."},
            },
        },
    },
    {
        "name": "list_pending_proposals",
        "description": "List all unresolved proposals awaiting user approval (short_id, title, created_at).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "resolve_proposal",
        "description": "Approve (y) or reject (n) a pending proposal. WRITE ACTION: the user will be asked to reply YES to confirm before this takes effect.",
        "input_schema": {
            "type": "object",
            "properties": {
                "short_id": {"type": "string", "description": "First 8 chars of the proposal id. Use list_pending_proposals to look up."},
                "approve": {"type": "boolean", "description": "true to approve, false to reject."},
                "reason": {"type": "string", "description": "Short note for the audit log."},
            },
            "required": ["short_id", "approve"],
        },
    },
    {
        "name": "propose_strategic_change",
        "description": "Raise a new proposal to the user (pause an agent, reallocate, activate kill switch, etc.). WRITE ACTION: the user will be asked to reply YES to confirm before the proposal is filed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short headline, e.g. 'Pause Atlas — regime flipped bearish'."},
                "details": {"type": "string", "description": "Full rationale: why, impact, what changes if approved."},
            },
            "required": ["title", "details"],
        },
    },
    {
        "name": "send_telegram_followup",
        "description": "Send an additional plain-text Telegram message to the user. Useful for 'fetching…' style updates while working through a multi-tool request.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
]


# ── Dispatch ──────────────────────────────────────────────────────────────────


async def _tool_get_positions(_args: dict[str, Any]) -> str:
    from ibkr.account import get_positions
    return json.dumps(await get_positions(), default=str)


async def _tool_get_balances(_args: dict[str, Any]) -> str:
    from ibkr.account import get_account_summary
    return json.dumps(await get_account_summary(), default=str)


async def _tool_get_pnl_summary(args: dict[str, Any]) -> str:
    from db import store
    period = args.get("period", "today")
    agent_name = args.get("agent_name")
    rows = await store.get_pnl_summary(agent_name=agent_name, period=period)
    return json.dumps(rows, default=str)


async def _tool_get_open_orders(_args: dict[str, Any]) -> str:
    from ibkr.account import get_open_orders
    return json.dumps(await get_open_orders(), default=str)


async def _tool_get_quote(args: dict[str, Any]) -> str:
    from data.massive_client import get_quote
    symbol = args["symbol"]
    return json.dumps(await get_quote(symbol), default=str)


async def _tool_get_agent_list(args: dict[str, Any]) -> str:
    from agent.agent_registry import list_agents
    enabled_only = bool(args.get("enabled_only", False))
    return json.dumps(list_agents(enabled_only=enabled_only), default=str)


async def _tool_get_mike_analysis(args: dict[str, Any]) -> str:
    """Read from the data/mike_analysis/ folder directly — don't go through MCP.
    Prefer the structured JSON if present; fall back to .txt.
    """
    from pathlib import Path
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        today_iso = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        from datetime import date as _d
        today_iso = _d.today().isoformat()

    date_arg = args.get("date", "today")
    date_iso = today_iso if date_arg == "today" else date_arg

    root = Path("data/mike_analysis")
    json_path = root / f"{date_iso}.json"
    txt_path = root / f"{date_iso}.txt"
    agent_name = args.get("agent_name")

    if json_path.exists():
        import json as _json
        data = _json.loads(json_path.read_text(encoding="utf-8"))
        if agent_name:
            key = f"{agent_name}_guidance"
            compact = {
                "date": date_iso,
                "regime": data.get("regime"),
                "risk_tone": data.get("risk_tone"),
                f"{agent_name}_guidance": data.get(key) or "(not specified)",
            }
            return _json.dumps(compact)
        return _json.dumps(data)

    if txt_path.exists():
        text = txt_path.read_text(encoding="utf-8")
        if len(text) > 4000:
            text = text[:4000] + "\n...[truncated]"
        return json.dumps({"date": date_iso, "format": "txt", "analysis": text})

    return json.dumps({"date": date_iso, "status": "not_found", "message": "No Mike analysis on file for this date."})


async def _tool_list_pending_proposals(_args: dict[str, Any]) -> str:
    from approval import proposals
    pending = [
        {"short_id": p["id"][:8], "title": p["title"], "created_at": p["created_at"],
         "ping_count": p.get("ping_count", 1)}
        for p in proposals.list_pending()
    ]
    return json.dumps(pending)


async def _tool_resolve_proposal(args: dict[str, Any]) -> str:
    """WRITE — but we stage an intent, do not execute. The router's
    confirm-gate must flip 'confirmed' before the call reaches _resolve.
    """
    from concierge.state import save_pending_confirm
    intent = {
        "kind": "resolve_proposal",
        "short_id": args["short_id"],
        "approve": bool(args.get("approve", False)),
        "reason": args.get("reason", "via concierge"),
    }
    save_pending_confirm(intent)
    verb = "APPROVE" if intent["approve"] else "REJECT"
    return json.dumps({
        "status": "pending_user_confirmation",
        "message": (
            f"Staged: will {verb} proposal {intent['short_id']}. "
            f"Your response to the user must ask them to reply YES to confirm, "
            f"or anything else to cancel."
        ),
    })


async def _tool_propose_strategic_change(args: dict[str, Any]) -> str:
    from concierge.state import save_pending_confirm
    intent = {
        "kind": "propose_strategic_change",
        "title": args["title"],
        "details": args["details"],
    }
    save_pending_confirm(intent)
    return json.dumps({
        "status": "pending_user_confirmation",
        "message": (
            "Staged: will file a new proposal titled "
            f"'{intent['title']}'. Your response must ask the user to reply "
            f"YES to confirm, or anything else to cancel."
        ),
    })


async def _tool_send_telegram_followup(args: dict[str, Any]) -> str:
    from approval.telegram import send_message
    await send_message(args["text"], parse_mode=None)  # plain text — no markdown parsing risk
    return json.dumps({"sent": True})


_DISPATCH = {
    "get_positions": _tool_get_positions,
    "get_balances": _tool_get_balances,
    "get_pnl_summary": _tool_get_pnl_summary,
    "get_open_orders": _tool_get_open_orders,
    "get_quote": _tool_get_quote,
    "get_agent_list": _tool_get_agent_list,
    "get_mike_analysis": _tool_get_mike_analysis,
    "list_pending_proposals": _tool_list_pending_proposals,
    "resolve_proposal": _tool_resolve_proposal,
    "propose_strategic_change": _tool_propose_strategic_change,
    "send_telegram_followup": _tool_send_telegram_followup,
}


async def dispatch(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Run a tool and return the result as a string (JSON when possible)."""
    handler = _DISPATCH.get(tool_name)
    if handler is None:
        return json.dumps({"error": f"unknown tool: {tool_name}"})
    try:
        return await handler(tool_input or {})
    except Exception as exc:
        log.exception("Tool %s failed", tool_name)
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


def filter_tools(allowed: list[str] | None) -> list[dict[str, Any]]:
    """Return only the schemas whose `name` is in `allowed`. None = all schemas."""
    if not allowed:
        return TOOL_SCHEMAS
    allowed_set = set(allowed)
    return [t for t in TOOL_SCHEMAS if t["name"] in allowed_set]


async def execute_confirmed_intent(intent: dict[str, Any]) -> dict[str, Any]:
    """Actually perform a write action after the user has confirmed via YES reply."""
    kind = intent.get("kind")
    if kind == "resolve_proposal":
        from approval import proposals
        fn = proposals._resolve  # private but stable
        result = fn(intent["short_id"], bool(intent.get("approve")), intent.get("reason", "via concierge"))
        if result:
            return {"ok": True, "kind": kind, "short_id": result["id"][:8], "status": result["status"]}
        return {"ok": False, "kind": kind, "reason": "no matching pending proposal"}
    if kind == "propose_strategic_change":
        from approval import proposals
        p = await proposals.create(title=intent["title"], details=intent["details"])
        return {"ok": True, "kind": kind, "short_id": p["id"][:8]}
    return {"ok": False, "reason": f"unknown intent kind: {kind}"}
