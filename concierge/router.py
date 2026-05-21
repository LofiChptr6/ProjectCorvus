"""Route inbound Telegram messages to the right handler.

Order:
  1. Outstanding write-action confirmation? — match YES/no, execute or cancel.
  2. y/n for a pending proposal — fast path, no LLM cost.
  3. /slash commands — cheap built-ins.
  4. Everything else — local-LLM concierge chat path.

Every inbound message is classified into a `kind` (user_text | slash_cmd |
approval) and recorded in the telegram_message log. Only kind='user_text' rows
flow into the concierge LLM's conversation context; the other kinds are
audit-only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

from approval.telegram import send_message
from concierge import chat, state, tools as concierge_tools
from db import store

log = logging.getLogger(__name__)


def _classify(text: str) -> str:
    """Return the telegram_message.kind for this inbound text (does not include
    the YES-confirmation special case — that path always carries the prior
    proposal_id intent in pending_confirm, so it's classified as 'approval')."""
    stripped = text.strip()
    lowered = stripped.lower()
    first_word = lowered.split()[0] if lowered else ""
    if first_word in ("y", "yes", "n", "no"):
        return "approval"
    if stripped.startswith("/"):
        return "slash_cmd"
    return "user_text"


async def route(
    text: str,
    cfg: dict[str, Any],
    *,
    chat_id: Optional[str] = None,
    telegram_message_id: Optional[int] = None,
    reply_to_message_id: Optional[int] = None,
    quote_text: Optional[str] = None,
) -> None:
    """Main dispatcher. Sends responses via Telegram.

    `reply_to_message_id` is the Telegram message_id this inbound text is
    replying to (when the user long-pressed a past bot message). `quote_text`
    is the optional highlighted fragment (Bot API 7.0+). Both are recorded
    on the inbound row and, for free-text turns, passed into `chat.handle`
    so the reply-resolver can pull the originating agent's context.
    """
    stripped = text.strip()
    lowered = stripped.lower()

    inbound_meta_extra: dict[str, Any] = {}
    if quote_text:
        inbound_meta_extra["quote_text"] = quote_text

    # 1. Pending write-action confirmation
    pending = state.load_pending_confirm()
    if pending is not None:
        try:
            await store.log_inbound(
                chat_id, telegram_message_id, "approval", stripped,
                meta={"event": "write_confirm_response",
                      "pending_kind": pending.get("kind"),
                      **inbound_meta_extra},
                reply_to_telegram_message_id=reply_to_message_id,
            )
        except Exception:
            log.debug("log_inbound (confirm) skipped", exc_info=True)
        await _handle_confirmation(pending, stripped)
        return

    kind = _classify(stripped)
    try:
        await store.log_inbound(
            chat_id, telegram_message_id, kind, stripped,
            meta=inbound_meta_extra or None,
            reply_to_telegram_message_id=reply_to_message_id,
        )
    except Exception:
        log.debug("log_inbound skipped", exc_info=True)

    # 2. y/n fast path
    if kind == "approval":
        await _handle_yn(stripped)
        return

    # 3. Slash commands
    if kind == "slash_cmd":
        await _handle_slash(stripped, cfg)
        return

    # 4. LLM chat
    reply = await chat.handle(
        stripped, cfg,
        chat_id=chat_id,
        reply_to_message_id=reply_to_message_id,
        quote_text=quote_text,
    )
    # chat.handle has already logged the assistant reply (and any tool rows)
    # to telegram_message with kind='concierge_reply' / 'concierge_tool', and
    # has sent the reply to Telegram. Nothing more to do here.
    if reply is None:
        # Defensive: chat.handle promises to send the reply. If it returned
        # None we fall back to a generic ack.
        await send_message(
            "(no reply produced)", parse_mode=None, kind="concierge_reply", role="assistant",
        )


# ── 1. Confirmation gate ──────────────────────────────────────────────────────


async def _handle_confirmation(pending: dict[str, Any], text: str) -> None:
    pending_kind = pending.get("kind")
    if text.strip().upper() == "YES":
        result = await concierge_tools.execute_confirmed_intent(pending)
        state.clear_pending_confirm()
        if result.get("ok"):
            if pending_kind == "resolve_proposal":
                verb = "approved" if pending.get("approve") else "rejected"
                await send_message(
                    f"✅ Proposal {result['short_id']} {verb}.",
                    parse_mode=None, kind="approval",
                    meta={"event": "write_confirm_resolved", "short_id": result["short_id"]},
                    source_ref={"kind": "proposal", "short_id": result["short_id"],
                                "event": "write_confirm_resolved",
                                "approved": bool(pending.get("approve"))},
                )
            elif pending_kind == "propose_strategic_change":
                await send_message(
                    f"📝 New proposal filed: {result['short_id']} — '{pending['title']}'. "
                    f"You'll get the approval ping momentarily.",
                    parse_mode=None, kind="approval",
                    meta={"event": "write_confirm_filed", "short_id": result["short_id"]},
                    source_ref={"kind": "proposal", "short_id": result["short_id"],
                                "title": pending.get("title"),
                                "event": "write_confirm_filed"},
                )
            elif pending_kind == "resolve_all_pending":
                resolved = result.get("resolved") or []
                if resolved:
                    body = "\n".join(f"• `{r['short_id']}` — {r['title']}" for r in resolved)
                    verb = "approved" if pending.get("approve") else "rejected"
                    await send_message(
                        f"✅ {len(resolved)} proposal(s) {verb} in bulk:\n{body}",
                        parse_mode=None, kind="approval",
                        meta={"event": "write_confirm_bulk", "approved": bool(pending.get("approve")), "count": len(resolved)},
                        source_ref={"kind": "proposal", "event": "write_confirm_bulk",
                                    "approved": bool(pending.get("approve")),
                                    "count": len(resolved),
                                    "short_ids": [r["short_id"] for r in resolved]},
                    )
                else:
                    await send_message(
                        "No pending proposals to action.",
                        parse_mode=None, kind="approval",
                        meta={"event": "write_confirm_bulk_noop"},
                    )
        else:
            await send_message(
                f"⚠️ Could not execute: {result.get('reason', 'unknown error')}",
                parse_mode=None, kind="approval",
                meta={"event": "write_confirm_failed", "pending_kind": pending_kind},
            )
    else:
        state.clear_pending_confirm()
        await send_message(
            "Cancelled — write action not executed. What would you like to do instead?",
            parse_mode=None, kind="approval",
            meta={"event": "write_confirm_cancelled", "pending_kind": pending_kind},
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
        await send_message(
            f"{icon} {verb}: `{p['id'][:8]}` — {p['title']}",
            kind="approval",
            meta={"event": "resolved_via_yn", "short_id": p["id"][:8], "approved": approved},
            source_ref={"kind": "proposal", "proposal_id": p["id"],
                        "proposal_kind": p.get("kind", "strategic_change"),
                        "title": p.get("title"),
                        "event": "resolved_via_yn", "approved": approved},
        )
    else:
        await send_message(
            "No pending proposal matches that reply. Use /proposals to see open items.",
            parse_mode=None, kind="approval",
            meta={"event": "yn_no_match"},
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
    "/relogin — restart IB Gateway (sends 2FA push to your phone)\n"
    "/help — this message\n\n"
    "Or just ask me anything in plain English."
)


async def _handle_slash(text: str, cfg: dict[str, Any]) -> None:
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    try:
        if cmd == "/help":
            await send_message(_HELP_TEXT, parse_mode=None, kind="slash_cmd", meta={"cmd": cmd})
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
        elif cmd == "/relogin":
            await _handle_relogin()
        else:
            await send_message(
                f"Unknown command: {cmd}. Try /help.",
                parse_mode=None, kind="slash_cmd",
                meta={"cmd": cmd, "event": "unknown_command"},
            )
    except Exception as exc:
        log.exception("Slash handler failed for %s", cmd)
        await send_message(
            f"⚠️ {cmd} failed: {type(exc).__name__}: {exc}",
            parse_mode=None, kind="slash_cmd",
            meta={"cmd": cmd, "event": "slash_handler_error"},
        )


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

    await send_message("\n".join(lines), kind="slash_cmd", meta={"cmd": "/status"})


async def _send_positions() -> None:
    from ibkr.account import get_positions
    positions = await get_positions()
    if not positions:
        await send_message("No open positions.", parse_mode=None, kind="slash_cmd", meta={"cmd": "/positions"})
        return
    lines = ["*Open positions:*"]
    for p in positions:
        lines.append(
            f"• {p.get('symbol', '?')}  qty {p.get('quantity', 0):+g}  "
            f"avg ${p.get('avg_cost', 0):.2f}  unreal {_fmt_pnl(p.get('unrealized_pnl', 0))}"
        )
    await send_message("\n".join(lines), kind="slash_cmd", meta={"cmd": "/positions"})


async def _send_pnl() -> None:
    from db import store
    rows = await store.get_pnl_summary(period="today")
    if not rows:
        await send_message("No P&L rows for today.", parse_mode=None, kind="slash_cmd", meta={"cmd": "/pnl"})
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
    await send_message("\n".join(lines), kind="slash_cmd", meta={"cmd": "/pnl"})


async def _send_proposals() -> None:
    from approval import proposals
    pending = proposals.list_pending()
    if not pending:
        await send_message("No pending proposals.", parse_mode=None, kind="slash_cmd", meta={"cmd": "/proposals"})
        return
    lines = ["*Pending proposals:*"]
    for p in pending:
        lines.append(f"• `{p['id'][:8]}` — {p['title']}")
    lines.append("")
    lines.append("Reply `y <short_id>` to approve or `n <short_id>` to reject.")
    await send_message("\n".join(lines), kind="slash_cmd", meta={"cmd": "/proposals"})


async def _raise_pause(agent: str) -> None:
    from approval import proposals
    if not agent:
        await send_message(
            "Usage: /pause <agent_name>  — e.g. /pause atlas",
            parse_mode=None, kind="slash_cmd", meta={"cmd": "/pause", "event": "usage"},
        )
        return
    title = f"Pause {agent} — requested via /pause"
    details = (
        f"Operator requested immediate pause of {agent} via Telegram /pause command. "
        f"Approving will disable the agent for the next scheduled run; rejecting keeps it live."
    )
    p = await proposals.create(title=title, details=details)
    await send_message(
        f"📝 Proposal {p['id'][:8]} filed — reply `y {p['id'][:8]}` to confirm pause.",
        kind="slash_cmd",
        meta={"cmd": "/pause", "agent": agent, "short_id": p["id"][:8]},
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
        parse_mode=None, kind="slash_cmd", meta={"cmd": "/budget"},
    )


async def _handle_relogin() -> None:
    """Restart ibgateway.service so IBC re-types credentials and triggers a
    fresh 2FA push. Operator taps the IBKR Mobile notification on their phone
    to complete login; the daemon's _reconnect_loop picks up once port 4001
    binds again. Requires the NOPASSWD sudoers entry at
    /etc/sudoers.d/ibgateway (see scripts/sudoers.d/ibgateway)."""
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", "systemctl", "restart", "ibgateway.service",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = (stderr or stdout).decode("utf-8", errors="replace").strip() or "unknown"
        msg = f"⚠️ /relogin failed: {err}"
        if "password is required" in err or "a terminal is required" in err:
            msg += (
                "\n\nInstall the sudoers entry:\n"
                "  sudo install -m 440 scripts/sudoers.d/ibgateway "
                "/etc/sudoers.d/ibgateway"
            )
        await send_message(
            msg, parse_mode=None, kind="slash_cmd",
            meta={"cmd": "/relogin", "event": "restart_failed", "rc": proc.returncode},
        )
        return

    await send_message(
        "🔄 Restarting IB Gateway. Tap the IBKR Mobile push on your phone to "
        "complete login — I'll confirm once port 4001 is back up.",
        parse_mode=None, kind="slash_cmd", meta={"cmd": "/relogin"},
    )
    asyncio.create_task(_relogin_followup())


async def _relogin_followup() -> None:
    """Poll port 4001 for up to 3 minutes after a /relogin, then notify."""
    deadline = asyncio.get_event_loop().time() + 180
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(5)
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", 4001), timeout=1
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            await send_message(
                "✅ Gateway is back on 4001. Daemon should be reconnected.",
                parse_mode=None, kind="slash_cmd",
                meta={"cmd": "/relogin", "event": "gateway_up"},
            )
            return
        except (OSError, asyncio.TimeoutError):
            continue
    await send_message(
        "⏱️ Gateway didn't return within 3 minutes. Check `journalctl -u ibgateway.service -n 60` on the box.",
        parse_mode=None, kind="slash_cmd",
        meta={"cmd": "/relogin", "event": "gateway_timeout"},
    )


# ── helpers ───────────────────────────────────────────────────────────────────


def _fmt_pnl(value: float | int | None) -> str:
    if value is None:
        return "$0.00"
    return f"${value:+,.2f}"
