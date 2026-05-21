"""Safe Telegram notifier for the Python pipeline.

Wraps approval.telegram.send_message so the runner can fire a per-skill
summary without crashing the pipeline if Telegram is unreachable / mis-
configured. Always logs failures and returns False; never raises.

────────────────────────────────────────────────────────────────────────────
DRY-RUN INTENTIONALLY FIRES TELEGRAM. THIS IS NOT A BUG.

If you came here because `write_summary.telegram_sent` was True on a
`--dry-run` invocation: that is by design. The contract is:

    dry-run fires *everything* except production-table writes.

Rationale: the Telegram path has its own moving parts (markdown render,
bot token, network, rate limits, message-size truncation). If we silenced
it under dry-run, every regression in that path would only surface on a
live run — when it's already too late. The `[DRY-RUN] ` prefix makes the
provenance visible to the human reading Telegram. Don't "fix" this by
gating the send on `not dry_run`. (Owner decision, recorded here so
nobody re-litigates the question.)
────────────────────────────────────────────────────────────────────────────
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
    subkind: str | None = None,
) -> bool:
    """Send `summary` to Telegram prefixed with the agent's name.

    `dry_run=True` prepends `[DRY-RUN] ` so the user can clearly distinguish
    pipeline-validation runs from live signal. The dry-run-fires-everything
    contract is the whole point — silent dry-runs hide regressions in the
    Telegram path itself.

    `subkind` (optional) is recorded into source_ref so the reply-resolver
    can tell apart e.g. an hourly review summary vs. a model-tune summary
    vs. an evening digest. Defaults to None.

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
    src: dict = {"kind": "agent_push", "author_agent": agent_name}
    if subkind:
        src["subkind"] = subkind
    try:
        await send_message(
            text, parse_mode="Markdown",
            meta={"author_agent": agent_name, "subkind": subkind} if subkind
                 else {"author_agent": agent_name},
            source_ref=src,
        )
        return True
    except Exception as e:
        log.warning("send_summary_safe(%s) failed: %s", agent_name, e)
        return False


async def send_chart_safe(
    image_path: str | None,
    caption: str | None,
    *,
    dry_run: bool = False,
    agent_name: str | None = None,
    subkind: str | None = None,
) -> bool:
    """Send a chart image (PNG/JPG) via Telegram sendPhoto.
    `dry_run=True` prepends `[DRY-RUN] ` to the caption.

    `agent_name` and `subkind` get carried in source_ref so a reply to the
    chart resolves to the right agent + flow (e.g. evening digest).
    """
    if not image_path or not caption:
        return False
    try:
        from approval.telegram import send_photo
    except Exception as e:
        log.warning("approval.telegram.send_photo unavailable: %s", e)
        return False
    prefix = DRY_RUN_PREFIX if dry_run else ""
    src: dict = {"kind": "agent_push", "chart_path": image_path}
    if agent_name:
        src["author_agent"] = agent_name
    if subkind:
        src["subkind"] = subkind
    try:
        result = await send_photo(
            image_path=image_path, caption=f"{prefix}{caption}",
            meta={"author_agent": agent_name or "system", "subkind": subkind}
                 if subkind else {"author_agent": agent_name or "system"},
            source_ref=src,
        )
        return result is not None
    except Exception as e:
        log.warning("send_chart_safe failed: %s", e)
        return False
