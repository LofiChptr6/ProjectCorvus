"""Route inbound Telegram messages to the right handler.

Order:
  1. Outstanding write-action confirmation? — match YES/no, execute or cancel.
  2. y/n for a pending proposal — fast path, no LLM cost.
  3. /slash commands — cheap built-ins.
  4. Everything else — Sonnet chat path.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from approval.telegram import send_message
from concierge import chat, state, tools as concierge_tools

log = logging.getLogger(__name__)


async def route(text: str, cfg: dict[str, Any]) -> None:
    """Main dispatcher. Sends responses via Telegram."""
    stripped = text.strip()
    lowered = stripped.lower()

    # 1. Pending write-action confirmation
    pending = state.load_pending_confirm()
    if pending is not None:
        await _handle_confirmation(pending, stripped)
        return

    # 2. y/n fast path
    first_word = lowered.split()[0] if lowered else ""
    if first_word in ("y", "yes", "n", "no"):
        await _handle_yn(stripped)
        return

    # 3. Slash commands
    if stripped.startswith("/"):
        await _handle_slash(stripped, cfg)
        return

    # 4. Sonnet chat
    reply = await chat.handle(stripped, cfg)
    await send_message(reply, parse_mode=None)


# ── 1. Confirmation gate ──────────────────────────────────────────────────────


async def _handle_confirmation(pending: dict[str, Any], text: str) -> None:
    if text.strip().upper() == "YES":
        result = await concierge_tools.execute_confirmed_intent(pending)
        state.clear_pending_confirm()
        if result.get("ok"):
            if pending["kind"] == "resolve_proposal":
                verb = "approved" if pending.get("approve") else "rejected"
                await send_message(f"✅ Proposal {result['short_id']} {verb}.", parse_mode=None)
            elif pending["kind"] == "propose_strategic_change":
                await send_message(
                    f"📝 New proposal filed: {result['short_id']} — '{pending['title']}'. "
                    f"You'll get the approval ping momentarily.",
                    parse_mode=None,
                )
        else:
            await send_message(
                f"⚠️ Could not execute: {result.get('reason', 'unknown error')}",
                parse_mode=None,
            )
    else:
        state.clear_pending_confirm()
        await send_message(
            "Cancelled — write action not executed. What would you like to do instead?",
            parse_mode=None,
        )


# ── 2. y/n fast path ──────────────────────────────────────────────────────────


async def _handle_yn(text: str) -> None:
    from approval import proposals
    parts = text.lower().split()
    verdict = parts[0]
    approved = verdict in ("y", "yes")
    if len(parts) >= 2:
        p = proposals._resolve(parts[1], approved, reason=f"Telegram reply: {text}")
    else:
        p = proposals._resolve_oldest(approved, reason=f"Telegram reply: {text}")
    if p:
        verb = "Approved" if approved else "Rejected"
        icon = "✅" if approved else "❌"
        await send_message(f"{icon} {verb}: `{p['id'][:8]}` — {p['title']}")
    else:
        await send_message(
            "No pending proposal matches that reply. Use /proposals to see open items.",
            parse_mode=None,
        )


# ── 3. Slash commands ─────────────────────────────────────────────────────────

_HELP_TEXT = (
    "Concierge commands:\n"
    "/status — positions + P&L + open orders + pending proposals\n"
    "/positions — current open positions\n"
    "/pnl — today's per-agent P&L\n"
    "/proposals — list pending approval items\n"
    "/pause <agent> — raise a proposal to pause an agent\n"
    "/budget — today's concierge API spend\n"
    "/help — this message\n\n"
    "Or just ask me anything in plain English."
)


async def _handle_slash(text: str, cfg: dict[str, Any]) -> None:
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    try:
        if cmd == "/help":
            await send_message(_HELP_TEXT, parse_mode=None)
        elif cmd == "/status":
            await _send_status()
        elif cmd == "/positions":
            await _send_positions()
        elif cmd == "/pnl":
            await _send_pnl()
        elif cmd == "/proposals":
            await _send_proposals()
        elif cmd == "/pause":
            await _raise_pause(arg.strip())
        elif cmd == "/budget":
            await _send_budget(cfg)
        else:
            await send_message(f"Unknown command: {cmd}. Try /help.", parse_mode=None)
    except Exception as exc:
        log.exception("Slash handler failed for %s", cmd)
        await send_message(f"⚠️ {cmd} failed: {type(exc).__name__}: {exc}", parse_mode=None)


async def _send_status() -> None:
    from ibkr.account import get_account_summary, get_positions, get_open_orders
    from db import store
    from approval import proposals

    try:
        summary = await get_account_summary()
    except Exception as exc:
        summary = {"error": str(exc)}
    try:
        positions = await get_positions()
    except Exception:
        positions = []
    try:
        orders = await get_open_orders()
    except Exception:
        orders = []
    try:
        pnl_rows = await store.get_pnl_summary(period="today")
    except Exception:
        pnl_rows = []

    lines = ["*Desk status*"]
    if "error" in summary:
        lines.append(f"Account: ⚠️ {summary['error']}")
    else:
        lines.append(
            f"NAV ${summary.get('nav', 0):,.0f} · Cash ${summary.get('cash', 0):,.0f} · "
            f"Day P&L {_fmt_pnl(summary.get('realized_pnl_today', 0))}"
        )

    if pnl_rows:
        lines.append("")
        lines.append("*Today by agent:*")
        for r in pnl_rows:
            pnl = (r.get("realized_pnl", 0) or 0) + (r.get("unrealized_pnl", 0) or 0)
            lines.append(f"• {r.get('agent_name', '?')}: {_fmt_pnl(pnl)}")

    if positions:
        lines.append("")
        lines.append(f"*Positions ({len(positions)}):*")
        for p in positions[:8]:
            lines.append(f"• {p.get('symbol', '?')} {p.get('quantity', 0):+g} @ ${p.get('avg_cost', 0):.2f}")
        if len(positions) > 8:
            lines.append(f"  …and {len(positions) - 8} more")
    else:
        lines.append("\n_No open positions._")

    if orders:
        lines.append("")
        lines.append(f"*Working orders: {len(orders)}*")

    pending = proposals.list_pending()
    if pending:
        lines.append("")
        lines.append(f"*Pending proposals: {len(pending)}*")
        for p in pending[:5]:
            lines.append(f"• `{p['id'][:8]}` — {p['title']}")

    await send_message("\n".join(lines))


async def _send_positions() -> None:
    from ibkr.account import get_positions
    positions = await get_positions()
    if not positions:
        await send_message("No open positions.", parse_mode=None)
        return
    lines = ["*Open positions:*"]
    for p in positions:
        lines.append(
            f"• {p.get('symbol', '?')}  qty {p.get('quantity', 0):+g}  "
            f"avg ${p.get('avg_cost', 0):.2f}  unreal {_fmt_pnl(p.get('unrealized_pnl', 0))}"
        )
    await send_message("\n".join(lines))


async def _send_pnl() -> None:
    from db import store
    rows = await store.get_pnl_summary(period="today")
    if not rows:
        await send_message("No P&L rows for today.", parse_mode=None)
        return
    lines = ["*Today's P&L by agent:*"]
    for r in rows:
        realized = r.get("realized_pnl", 0) or 0
        unreal = r.get("unrealized_pnl", 0) or 0
        total = realized + unreal
        lines.append(
            f"• {r.get('agent_name', '?')}: total {_fmt_pnl(total)}  "
            f"(realized {_fmt_pnl(realized)}, unreal {_fmt_pnl(unreal)})"
        )
    await send_message("\n".join(lines))


async def _send_proposals() -> None:
    from approval import proposals
    pending = proposals.list_pending()
    if not pending:
        await send_message("No pending proposals.", parse_mode=None)
        return
    lines = ["*Pending proposals:*"]
    for p in pending:
        lines.append(f"• `{p['id'][:8]}` — {p['title']}")
    lines.append("")
    lines.append("Reply `y <short_id>` to approve or `n <short_id>` to reject.")
    await send_message("\n".join(lines))


async def _raise_pause(agent: str) -> None:
    from approval import proposals
    if not agent:
        await send_message("Usage: /pause <agent_name>  — e.g. /pause atlas", parse_mode=None)
        return
    title = f"Pause {agent} — requested via /pause"
    details = (
        f"Operator requested immediate pause of {agent} via Telegram /pause command. "
        f"Approving will disable the agent for the next scheduled run; rejecting keeps it live."
    )
    p = await proposals.create(title=title, details=details)
    await send_message(
        f"📝 Proposal {p['id'][:8]} filed — reply `y {p['id'][:8]}` to confirm pause.",
    )


async def _send_budget(cfg: dict[str, Any]) -> None:
    import os
    u = state.load_usage()
    cap = int(
        cfg.get("daily_token_cap", 0)
        or os.environ.get("CONCIERGE_DAILY_TOKEN_CAP", 0)
        or 0
    )
    total = u.get("input_tokens", 0) + u.get("output_tokens", 0)
    cap_str = f" / {cap:,}" if cap else ""
    await send_message(
        f"Concierge today: {total:,} tokens{cap_str}\n"
        f"{u.get('requests', 0)} requests · "
        f"{u.get('input_tokens', 0):,}in / {u.get('output_tokens', 0):,}out",
        parse_mode=None,
    )


# ── helpers ───────────────────────────────────────────────────────────────────


def _fmt_pnl(value: float | int | None) -> str:
    if value is None:
        return "$0.00"
    return f"${value:+,.2f}"
