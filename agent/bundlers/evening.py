"""Bundler for `*-evening` skills — end-of-day attribution review.

EOD context is fully knowable upfront (no per-symbol research needed at this
hour), so the runner runs a single round-trip: bundle → render → LLM →
structured-output (EveningOutput) → orchestrator generates slide + Telegram.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timezone
from typing import Any, Optional

from agent.bundlers.common import load_journal_split, read_workspace
from db import store

log = logging.getLogger(__name__)


@dataclass
class EveningBundle:
    agent_name: str
    trading_date_iso: str
    pnl_today: Optional[dict[str, Any]] = None  # {realized, unrealized, total, n_positions, ...}
    pnl_summary_today: list[dict[str, Any]] = field(default_factory=list)
    pnl_summary_week: list[dict[str, Any]] = field(default_factory=list)
    journal_open: list[dict[str, Any]] = field(default_factory=list)
    journal_due: list[dict[str, Any]] = field(default_factory=list)
    journal_resolutions: list[dict[str, Any]] = field(default_factory=list)
    active_views: list[dict[str, Any]] = field(default_factory=list)
    workspace: dict[str, Any] = field(default_factory=dict)
    bundle_warnings: list[str] = field(default_factory=list)


async def _safe(coro, default, warnings: list[str], label: str):
    try:
        return await coro
    except Exception as e:
        warnings.append(f"{label}: {type(e).__name__}: {e}")
        return default


async def get_evening_bundle(agent_name: str) -> EveningBundle:
    warnings: list[str] = []
    today_iso = _date.today().isoformat()

    workspace = read_workspace(agent_name)

    # Combined P&L (realized + unrealized) — best-effort.
    pnl_today: Optional[dict[str, Any]] = None
    try:
        from reporting.agent_pnl import get_pnl_combined
        pnl_today = await get_pnl_combined(agent_name=agent_name)
    except Exception as e:
        warnings.append(f"pnl_combined: {type(e).__name__}: {e}")

    pnl_today_summary = await _safe(
        store.get_pnl_summary(agent_name=agent_name, period="today"),
        [], warnings, "pnl_summary_today",
    )
    pnl_week_summary = await _safe(
        store.get_pnl_summary(agent_name=agent_name, period="week"),
        [], warnings, "pnl_summary_week",
    )

    journal = await _safe(
        load_journal_split(agent_name),
        {"open": [], "due_today_or_earlier": [], "recent_resolutions": []},
        warnings, "journal",
    )

    active_views = await _safe(
        store.get_agent_active_convictions(agent_name),
        [], warnings, "active_views",
    )

    return EveningBundle(
        agent_name=agent_name,
        trading_date_iso=today_iso,
        pnl_today=pnl_today,
        pnl_summary_today=pnl_today_summary,
        pnl_summary_week=pnl_week_summary,
        journal_open=journal["open"],
        journal_due=journal["due_today_or_earlier"],
        journal_resolutions=journal["recent_resolutions"],
        active_views=active_views,
        workspace=workspace,
        bundle_warnings=warnings,
    )
