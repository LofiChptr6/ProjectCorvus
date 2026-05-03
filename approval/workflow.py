"""Human-in-the-loop approval workflow via Telegram."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class ApprovalResult:
    approved: bool
    reason: str = ""
    approver: str = "human"


async def request_approval(
    order,
    estimated_notional: float,
    session_id: Optional[str],
    cfg: dict,
) -> ApprovalResult:
    """Send Telegram approval request. Block until reply or timeout."""
    from approval.telegram import send_message, poll_for_reply, escape_markdown
    from approval import bypass

    if bypass.is_active():
        note = bypass.reason()
        log.info("Approval auto-granted: %s on %s %s x%s — %s",
                 order.action, order.symbol, order.action, order.quantity, note)
        try:
            await send_message(
                f"⚡ Auto-approved ({note}): {order.action} {order.symbol} "
                f"x{order.quantity:,.0f} (~${estimated_notional:,.0f})",
                parse_mode=None,
            )
        except Exception:
            pass
        return ApprovalResult(approved=True, reason=note, approver="bypass")

    timeout_s = cfg.get("approval", {}).get("timeout_s", 120)
    mode = cfg.get("trading", {}).get("mode", "paper")

    # Escape every agent-controlled field before interpolating into Markdown.
    # Cap reasoning so a runaway agent rationale can't blow past Telegram's
    # 4096-char message limit and force-fail this approval.
    safe_agent = escape_markdown(str(order.agent_name or ""))
    safe_symbol = escape_markdown(str(order.symbol or ""))
    safe_action = escape_markdown(str(order.action or ""))
    safe_order_type = escape_markdown(str(order.order_type or ""))
    safe_reasoning = escape_markdown(str(order.reasoning or "")[:300])

    text = (
        f"🚨 *TRADE APPROVAL REQUIRED* [{mode.upper()}]\n\n"
        f"*Agent:* {safe_agent}\n"
        f"*Symbol:* {safe_symbol}\n"
        f"*Action:* {safe_action}\n"
        f"*Quantity:* {order.quantity:,.0f} shares\n"
        f"*Order type:* {safe_order_type}\n"
        f"*Price:* ${order.effective_price or 'MKT':,.2f}\n"
        f"*Notional:* ~${estimated_notional:,.0f}\n\n"
        f"*Reasoning:*\n{safe_reasoning}\n\n"
        f"⚠️ Reply with ONLY one of: `y`, `yes`, `n`, `no` "
        f"(any other text is ignored — auto-rejects after {timeout_s}s)"
    )

    log.info("Sending Telegram approval request for %s %s %s", order.action, order.symbol, order.quantity)
    response = await send_message(text)

    if not response or not response.get("ok"):
        log.warning("Telegram send failed — auto-rejecting for safety")
        return ApprovalResult(approved=False, reason="Telegram send failed; rejected for safety.")

    sent_message_id = response["result"]["message_id"]
    reply = await poll_for_reply(sent_message_id, timeout_s=timeout_s)

    if reply in ("y", "yes"):
        log.info("Trade approved via Telegram")
        await send_message(f"✅ Approved — submitting {order.action} {order.symbol} x{order.quantity:,.0f}")
        return ApprovalResult(approved=True, reason="Human approved via Telegram")

    if reply in ("n", "no"):
        log.info("Trade rejected via Telegram")
        await send_message(f"❌ Rejected — order cancelled")
        return ApprovalResult(approved=False, reason="Human rejected via Telegram")

    # Timeout
    log.warning("Approval timed out after %ds — rejecting", timeout_s)
    await send_message(f"⏱ Timeout — trade auto-rejected after {timeout_s}s")
    return ApprovalResult(approved=False, reason=f"Approval timed out after {timeout_s}s")
