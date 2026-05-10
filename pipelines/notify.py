"""Safe Telegram notifier for the Python pipeline.

Wraps approval.telegram.send_message so the runner can fire a per-skill
summary without crashing the pipeline if Telegram is unreachable / mis-
configured. Always logs failures and returns False; never raises.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def send_summary_safe(agent_name: str, summary: str | None) -> bool:
    """Send `summary` to Telegram prefixed with the agent's name.

    Returns True on a successful send, False otherwise. Empty / None
    summary is a no-op (False). All exceptions are caught and logged.
    """
    if not summary or not summary.strip():
        return False
    try:
        from approval.telegram import send_message
    except Exception as e:
        log.warning("approval.telegram unavailable: %s", e)
        return False
    text = f"*{agent_name}*: {summary.strip()}"
    try:
        await send_message(text, parse_mode="Markdown")
        return True
    except Exception as e:
        log.warning("send_summary_safe(%s) failed: %s", agent_name, e)
        return False
