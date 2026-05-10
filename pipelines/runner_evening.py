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


async def _send_telegram_safe(chart_path: Optional[str], caption: Optional[str]) -> bool:
    if not chart_path or not caption:
        return False
    try:
        from approval.telegram import send_chart
        await send_chart(image_path=chart_path, caption=caption)
        return True
    except Exception as e:
        log.warning("send_telegram_chart skipped: %s", e)
        return False


async def apply_evening_output(
    parsed: EveningOutput,
    *,
    agent_name: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "digest_id": None,
        "chart_path": None,
        "telegram_sent": False,
        "theses_recorded": 0,
        "theses_graded": 0,
    }

    chart_path = None
    if not dry_run:
        chart_path = await _generate_slide_safe(agent_name, parsed)
        summary["chart_path"] = chart_path
        summary["telegram_sent"] = await _send_telegram_safe(chart_path, parsed.telegram_caption)

    # Always record the digest — it's the durable record of the LLM's grading.
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

    if not dry_run:
        for t in parsed.theses_to_record:
            await store.record_thesis(
                agent_name=agent_name, kind=t.kind,
                title=t.title, body=t.body, verify_by=t.verify_by,
                parent_id=t.parent_id, market_snapshot=t.market_snapshot,
            )
            summary["theses_recorded"] += 1
        for g in parsed.theses_to_grade:
            await store.update_thesis_status(
                thesis_id=g.thesis_id, status=g.status,
                resolution_note=g.resolution_note, agent_name=agent_name,
            )
            summary["theses_graded"] += 1

    return summary
