"""Evening write path: takes the validated EveningOutput and emits the side
effects — record_evening_digest, optional generate_evening_slide, optional
Telegram. Each side effect is best-effort (slide/Telegram don't block the
digest write)."""
from __future__ import annotations

import logging
from datetime import date as _date
from typing import Any, Optional

from db import store
from pipelines.schemas import EveningOutput

log = logging.getLogger(__name__)


async def _generate_slide_safe(
    agent_name: str, output: EveningOutput,
) -> Optional[str]:
    try:
        from reporting.evening_slide import generate_evening_slide
    except Exception as e:
        log.warning("generate_evening_slide unavailable: %s", e)
        return None
    try:
        result = await generate_evening_slide(
            agent_name=agent_name,
            headline=output.headline,
            trends=output.trends,
            theses=output.theses,
            philosophy=output.philosophy,
            open_questions=output.open_questions,
        )
        if isinstance(result, dict):
            return result.get("chart_path")
        if isinstance(result, str):
            return result
    except Exception as e:
        log.warning("generate_evening_slide failed: %s", e)
    return None


async def apply_evening_output(
    parsed: EveningOutput,
    *,
    agent_name: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Dry-run = live in everything except `dry_run` flag on the digest row.

    Slide generation, Telegram chart send, thesis write/grade, digest write all
    fire in both modes. The Telegram caption is `[DRY-RUN]`-prefixed in dry-run
    so the user can distinguish at a glance.
    """
    from pipelines import notify

    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "digest_id": None,
        "chart_path": None,
        "telegram_sent": False,
        "theses_recorded": 0,
        "theses_graded": 0,
    }

    # Always generate the slide. Slides are EOD artifacts the user actively
    # looks for; they don't affect trading. _generate_slide_safe returns None
    # if the renderer isn't available (e.g. in tests with stubbed deps).
    chart_path = await _generate_slide_safe(agent_name, parsed)
    summary["chart_path"] = chart_path

    # Telegram with [DRY-RUN] prefix in dry-run.
    summary["telegram_sent"] = await notify.send_chart_safe(
        chart_path, parsed.telegram_caption, dry_run=dry_run,
        agent_name=agent_name, subkind="evening_digest",
    )

    # Digest row — durable record of the grading regardless of mode.
    today_iso = _date.today().isoformat()
    digest_id = await store.record_evening_digest(
        agent_name=agent_name,
        trading_date=today_iso,
        thesis_summary="\n".join(parsed.theses) if parsed.theses else None,
        open_questions="\n".join(parsed.open_questions) if parsed.open_questions else None,
        tomorrow_focus=parsed.headline,
        pnl_today=parsed.pnl_today,
        pnl_week=parsed.pnl_week,
        chart_path=chart_path,
    )
    summary["digest_id"] = digest_id

    # Theses + grading — append-only audit, fires in both modes.
    for t in parsed.theses_to_record:
        await store.record_thesis(
            agent_name=agent_name, kind=t.kind,
            title=t.title, body=t.body, verify_by=t.verify_by,
            parent_id=t.parent_id, market_snapshot=t.market_snapshot,
            primary_symbol=t.primary_symbol,
            direction=t.direction,
            entry_price=t.entry_price,
        )
        summary["theses_recorded"] += 1
    for g in parsed.theses_to_grade:
        await store.update_thesis_status(
            thesis_id=g.thesis_id, status=g.status,
            resolution_note=g.resolution_note, agent_name=agent_name,
        )
        summary["theses_graded"] += 1

    return summary
