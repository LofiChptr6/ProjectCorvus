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
from typing import Any, Optional

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
    available_models: list[dict[str, Any]] = field(default_factory=list)
    # Phase B of CITATION_ARCH (2026-05-21): registered agent-callable skills.
    # Each entry: {name, version, description}. The LLM picks a `skill_name`
    # and calls the `run_skill` MCP tool to get an answer with an evidence_id
    # it can attach to a Citation.
    available_skills: list[dict[str, Any]] = field(default_factory=list)
    bundle_warnings: list[str] = field(default_factory=list)
    # Queue-driven invocations (queue_worker subprocess) carry job context
    # so the agent knows what woke it — OCAP triggers, specific ticker focus,
    # or just a routine hourly prime. None when invoked directly (e.g. legacy
    # cron path or manual CLI).
    job_context: Optional[dict[str, Any]] = None
    pending_inbox: list[dict[str, Any]] = field(default_factory=list)


async def _load_universe(agent_name: str) -> list[str]:
    """Active watchlist symbols for one agent, from the agent_watchlist table."""
    try:
        rows = await store.load_agent_watchlist(agent_name)
    except Exception:
        return []
    return [r["symbol"] for r in rows]


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


async def get_review_bundle(
    agent_name: str, *, job_id: Optional[int] = None,
) -> ReviewBundle:
    """Build the hourly-review context bundle. `job_id` (when invoked from
    the queue worker) loads `agent_job.triggers_seen` + `payload` so the
    agent can see what woke this specific review (OCAP rule, routine prime,
    one-shot manual request)."""
    warnings: list[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    workspace = await read_workspace(agent_name)
    universe = await _load_universe(agent_name)
    journal = await _safe(load_journal_split(agent_name), {"open": [], "due_today_or_earlier": [], "recent_resolutions": []}, warnings, "journal")
    active_views = await _safe(store.get_agent_active_convictions(agent_name), [], warnings, "active_views")

    # Pending inbox (user / OCAP / sibling-agent messages waiting for this
    # agent). The respond skill drains these, but the review skill should
    # see them too — otherwise dashboard questions sit until the next
    # respond cycle, and OCAP wake-up notes never reach the review prompt.
    pending_inbox: list[dict[str, Any]] = []
    try:
        if hasattr(store, "get_pending_inbox"):
            pending_inbox = await store.get_pending_inbox(agent_name, limit=10)
    except Exception as e:
        warnings.append(f"pending_inbox: {type(e).__name__}: {e}")

    # Queue-driven invocations carry job context. The worker sets JOB_ID env
    # var (or callers pass job_id directly). When present, load the row so
    # the agent can render "I was woken by OCAP on SPY tripping rolling_std_breach".
    job_context: Optional[dict[str, Any]] = None
    if job_id is not None:
        try:
            from db.schema import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                jr = await conn.fetchrow(
                    """SELECT id, job_type, priority, payload, triggers_seen,
                              enqueued_at, started_at
                       FROM agent_job WHERE id=$1""",
                    int(job_id),
                )
                if jr:
                    job_context = dict(jr)
        except Exception as e:
            warnings.append(f"job_context: {type(e).__name__}: {e}")

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

    available_models: list[dict[str, Any]] = []
    try:
        from meta_agent.conviction_from_model import discover_agent_models
        available_models = discover_agent_models(agent_name)
    except Exception as e:
        warnings.append(f"available_models: {type(e).__name__}: {e}")

    available_skills: list[dict[str, Any]] = []
    try:
        from meta_agent.skill_loader import list_agent_skills
        available_skills = list_agent_skills(agent_name)
    except Exception as e:
        warnings.append(f"available_skills: {type(e).__name__}: {e}")

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
        available_models=available_models,
        available_skills=available_skills,
        bundle_warnings=warnings,
        job_context=job_context,
        pending_inbox=pending_inbox,
    )
