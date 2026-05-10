"""Bundler for `*-respond` skills.

Pulls everything the respond template needs in one shot — pending inbox,
workspace, active views, journal — instead of forcing the LLM to chain 4
MCP calls before its first reasoning token.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.bundlers.common import load_journal_split, read_workspace
from db import store


@dataclass
class RespondBundle:
    agent_name: str
    pending_inbox: list[dict[str, Any]]
    workspace: dict[str, Any]
    active_views: list[dict[str, Any]]
    journal_open: list[dict[str, Any]] = field(default_factory=list)
    journal_due: list[dict[str, Any]] = field(default_factory=list)
    journal_resolutions: list[dict[str, Any]] = field(default_factory=list)


async def get_respond_bundle(agent_name: str) -> RespondBundle:
    pending = await store.get_pending_inbox(agent_name)
    if not pending:
        # Caller (runner) checks pending_inbox empty and short-circuits — no
        # point loading the rest of the bundle for an empty inbox.
        return RespondBundle(
            agent_name=agent_name,
            pending_inbox=[],
            workspace={},
            active_views=[],
        )

    workspace = read_workspace(agent_name)
    views = await store.get_agent_active_convictions(agent_name)
    journal = await load_journal_split(agent_name)

    return RespondBundle(
        agent_name=agent_name,
        pending_inbox=pending,
        workspace=workspace,
        active_views=views,
        journal_open=journal["open"],
        journal_due=journal["due_today_or_earlier"],
        journal_resolutions=journal["recent_resolutions"],
    )
