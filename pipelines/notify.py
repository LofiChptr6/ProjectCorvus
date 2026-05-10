"""Safe Telegram notifier for the Python pipeline.

Wraps approval.telegram.send_message so the runner can fire a per-skill
summary without crashing the pipeline if Telegram is unreachable / mis-
configured. Always logs failures and returns False; never raises.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


DRY_RUN_PREFIX = "[DRY-RUN] "


async def send_summary_safe(
    agent_name: str,
    summary: str | None,
    *,
    dry_run: bool = False,
) -> bool:
    """Send `summary` to Telegram prefixed with the agent's name.

    `dry_run=True` prepends `[DRY-RUN] ` so the user can clearly distinguish
    pipeline-validation runs from live signal. The dry-run-fires-everything
    contract is the whole point — silent dry-runs hide regressions in the
    Telegram path itself.

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
    prefix = DRY_RUN_PREFIX if dry_run else ""
    text = f"{prefix}*{agent_name}*: {summary.strip()}"
    try:
        await send_message(text, parse_mode="Markdown")
        return True
    except Exception as e:
        log.warning("send_summary_safe(%s) failed: %s", agent_name, e)
        return False


async def send_chart_safe(
    image_path: str | None,
    caption: str | None,
    *,
    dry_run: bool = False,
) -> bool:
    """Send a chart image (PNG/JPG) via Telegram sendPhoto.
    `dry_run=True` prepends `[DRY-RUN] ` to the caption."""
    if not image_path or not caption:
        return False
    try:
        from approval.telegram import send_photo
    except Exception as e:
        log.warning("approval.telegram.send_photo unavailable: %s", e)
        return False
    prefix = DRY_RUN_PREFIX if dry_run else ""
    try:
        result = await send_photo(image_path=image_path, caption=f"{prefix}{caption}")
        return result is not None
    except Exception as e:
        log.warning("send_chart_safe failed: %s", e)
        return False
