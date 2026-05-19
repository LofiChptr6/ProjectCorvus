"""Bundler for `*-model-tune` skills — weekly model portfolio audit + evolve.

Loads everything the LLM needs to decide what to tune/add/scrap: existing
model files (full source), hypothesis log, journal, attribution, recent bars,
fresh `compute_all_models` snapshot. Bundle is intentionally rich — token
spend on this weekly skill is small relative to the value of avoiding
repeated investigation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agent.bundlers.common import load_journal_split, read_workspace
from db import store

log = logging.getLogger(__name__)


@dataclass
class ModelTuneBundle:
    agent_name: str
    sector_yaml: str  # raw text
    universe: list[str]
    workspace: dict[str, Any]
    model_files: dict[str, str] = field(default_factory=dict)  # filename → full content
    hypothesis_log: str = ""
    journal_open: list[dict[str, Any]] = field(default_factory=list)
    journal_open_models: list[dict[str, Any]] = field(default_factory=list)
    journal_due: list[dict[str, Any]] = field(default_factory=list)
    journal_resolutions: list[dict[str, Any]] = field(default_factory=list)
    active_views: list[dict[str, Any]] = field(default_factory=list)
    sector_stories: list[dict[str, Any]] = field(default_factory=list)
    bundle_warnings: list[str] = field(default_factory=list)


def _read_models_dir(agent_name: str) -> dict[str, str]:
    base = Path("agents") / agent_name / "models"
    if not base.is_dir():
        return {}
    out: dict[str, str] = {}
    for f in sorted(base.iterdir()):
        if not f.is_file() or f.suffix != ".py" or f.name == "__init__.py":
            continue
        try:
            out[f.name] = f.read_text(encoding="utf-8")
        except Exception as e:
            out[f.name] = f"(failed to read: {type(e).__name__}: {e})"
    return out


def _read_text_safe(path: Path, default: str = "") -> str:
    if not path.is_file():
        return default
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        return f"(failed to read: {type(e).__name__}: {e})"


async def _load_universe(agent_name: str) -> list[str]:
    """Active watchlist symbols for one agent, from the agent_watchlist table."""
    try:
        rows = await store.load_agent_watchlist(agent_name)
    except Exception:
        return []
    return [r["symbol"] for r in rows]


async def get_model_tune_bundle(agent_name: str) -> ModelTuneBundle:
    warnings: list[str] = []

    sector_yaml = _read_text_safe(Path("agents") / f"{agent_name}.yaml")
    universe = await _load_universe(agent_name)
    workspace = await read_workspace(agent_name)

    model_files = _read_models_dir(agent_name)
    hypothesis_log = _read_text_safe(
        Path("agents") / agent_name / "notes" / "model_hypothesis.md",
    )

    try:
        journal = await load_journal_split(agent_name)
    except Exception as e:
        warnings.append(f"journal: {type(e).__name__}: {e}")
        journal = {"open": [], "due_today_or_earlier": [], "recent_resolutions": []}

    open_models = [t for t in journal["open"]
                   if isinstance(t.get("title"), str) and t["title"].startswith("model:")]

    try:
        active_views = await store.get_agent_active_convictions(agent_name)
    except Exception as e:
        warnings.append(f"active_views: {type(e).__name__}: {e}")
        active_views = []

    sector_stories: list[dict[str, Any]] = []
    try:
        from db.schema import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT period_start, period_end, narrative
                   FROM sector_story WHERE agent_name=$1
                   ORDER BY period_end DESC LIMIT 4""",
                agent_name,
            )
            sector_stories = [dict(r) for r in rows]
    except Exception as e:
        warnings.append(f"sector_stories: {type(e).__name__}: {e}")

    return ModelTuneBundle(
        agent_name=agent_name,
        sector_yaml=sector_yaml,
        universe=universe,
        workspace=workspace,
        model_files=model_files,
        hypothesis_log=hypothesis_log,
        journal_open=journal["open"],
        journal_open_models=open_models,
        journal_due=journal["due_today_or_earlier"],
        journal_resolutions=journal["recent_resolutions"],
        active_views=active_views,
        sector_stories=sector_stories,
        bundle_warnings=warnings,
    )
