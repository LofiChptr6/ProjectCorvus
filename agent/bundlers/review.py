"""Bundler for `*-review` skills.

Heaviest bundle: account state, positions, P&L, journal, mike-analysis,
sector stories, watchlist, recent active views, attribution, universe. Most of
this is what `agent.prompt_builder.build_context_message` already gathers — we
reuse it via `agent_context_text` and supplement with sector-specific data
(active views from last hour, attribution, sector stories, journal split).

Robustness: bundlers must NOT crash if IBKR is unreachable or the desk hasn't
booted yet. Each section catches and degrades to an empty/None placeholder so
the LLM sees a recognizable structure even in degraded mode.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from agent.bundlers.common import load_journal_split, read_workspace
from db import store

log = logging.getLogger(__name__)


@dataclass
class ReviewBundle:
    agent_name: str
    now_iso: str
    universe: list[str]
    workspace: dict[str, Any]
    agent_context_text: str  # output of build_context_message — full account + state
    journal_open: list[dict[str, Any]] = field(default_factory=list)
    journal_due: list[dict[str, Any]] = field(default_factory=list)
    journal_resolutions: list[dict[str, Any]] = field(default_factory=list)
    active_views: list[dict[str, Any]] = field(default_factory=list)
    sector_stories: list[dict[str, Any]] = field(default_factory=list)
    pnl_attribution: Optional[dict[str, Any]] = None
    market_status: Optional[dict[str, Any]] = None
    quiet_window: Optional[bool] = None
    kill_switch: Optional[dict[str, Any]] = None
    bundle_warnings: list[str] = field(default_factory=list)


def _load_universe(agent_name: str) -> list[str]:
    sector_map = Path("agents/sector_map.yaml")
    if not sector_map.is_file():
        return []
    try:
        data = yaml.safe_load(sector_map.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    return list(data.get("agents", {}).get(agent_name, {}).get("universe", []))


async def _safe(coro, default, warnings: list[str], label: str):
    try:
        return await coro
    except Exception as e:
        warnings.append(f"{label}: {type(e).__name__}: {e}")
        return default


async def _agent_context_text(agent_name: str, warnings: list[str]) -> str:
    """Reuse build_context_message + build_system_prompt for the canonical
    account/positions/fills/journal blob the harness path already feeds agents.

    On failure, return an empty string and record a warning — the rest of the
    bundle still works without it (agent just sees less context).
    """
    try:
        from agent.agent_registry import load_agent
        from agent.prompt_builder import build_context_message
        agent_cfg = load_agent(agent_name)
        return await build_context_message(agent_cfg, "review")
    except Exception as e:
        warnings.append(f"agent_context_text: {type(e).__name__}: {e}")
        return ""


async def get_review_bundle(agent_name: str) -> ReviewBundle:
    warnings: list[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    workspace = read_workspace(agent_name)
    universe = _load_universe(agent_name)
    journal = await _safe(load_journal_split(agent_name), {"open": [], "due_today_or_earlier": [], "recent_resolutions": []}, warnings, "journal")
    active_views = await _safe(store.get_agent_active_convictions(agent_name), [], warnings, "active_views")

    # Optional / best-effort sections.
    sector_stories = []
    try:
        # Sector stories live in the sector_story table per db/schema.py.
        from db.schema import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT period_start, period_end, narrative, stats_json
                   FROM sector_story WHERE agent_name=$1
                   ORDER BY period_end DESC LIMIT 4""",
                agent_name,
            )
            sector_stories = [dict(r) for r in rows]
    except Exception as e:
        warnings.append(f"sector_stories: {type(e).__name__}: {e}")

    pnl_attribution = None
    try:
        # Optional: only call if the function exists.
        if hasattr(store, "get_agent_pnl_attribution"):
            pnl_attribution = await store.get_agent_pnl_attribution(agent_name)
    except Exception as e:
        warnings.append(f"pnl_attribution: {type(e).__name__}: {e}")

    # Skip-fast signals — best effort.
    market_status = None
    quiet_window = None
    kill_switch = None
    try:
        kill_switch = {"is_active": await store.is_killed(agent_name)}
    except Exception as e:
        warnings.append(f"kill_switch: {type(e).__name__}: {e}")

    agent_context_text = await _agent_context_text(agent_name, warnings)

    return ReviewBundle(
        agent_name=agent_name,
        now_iso=now_iso,
        universe=universe,
        workspace=workspace,
        agent_context_text=agent_context_text,
        journal_open=journal["open"],
        journal_due=journal["due_today_or_earlier"],
        journal_resolutions=journal["recent_resolutions"],
        active_views=active_views,
        sector_stories=sector_stories,
        pnl_attribution=pnl_attribution,
        market_status=market_status,
        quiet_window=quiet_window,
        kill_switch=kill_switch,
        bundle_warnings=warnings,
    )
