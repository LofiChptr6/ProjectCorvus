"""obs/dashboard.py — chat-input helpers.

The sync wrappers (post_question, _recent_qa) call asyncio.run() internally,
which can't be invoked from within an existing event loop. Tests therefore
exercise the underlying *_async helpers directly. The fire_respond_subprocess
test is sync (no event loop running) and works.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


async def test_post_question_async_writes_inbox_row(test_agent):
    from db import store
    from obs import dashboard

    inbox_id = await dashboard._post_question_async(test_agent, "what's your read on XLE?", sender="user")
    assert isinstance(inbox_id, int) and inbox_id > 0

    pending = await store.get_pending_inbox(test_agent)
    assert len(pending) == 1
    assert pending[0]["body"] == "what's your read on XLE?"
    assert pending[0]["sender"] == "user"


async def test_recent_qa_async_returns_recent_rows(test_agent):
    from db import store
    from obs import dashboard

    await store.post_to_inbox(test_agent, "Q1")
    await store.post_to_inbox(test_agent, "Q2")
    rows = await dashboard._recent_qa_async(test_agent, limit=5)
    bodies = [r["body"] for r in rows]
    # Most recent first.
    assert bodies[0] == "Q2"
    assert bodies[1] == "Q1"


async def test_post_then_mark_responded_round_trip(test_agent):
    from db import store
    from obs import dashboard

    inbox_id = await dashboard._post_question_async(test_agent, "what changed today?")
    await store.mark_inbox_responded(
        inbox_id=inbox_id,
        response_body="momentum stack flipped at noon",
        agent_name=test_agent,
    )

    recent = await dashboard._recent_qa_async(test_agent, limit=5)
    assert len(recent) == 1
    assert recent[0]["response_body"] == "momentum stack flipped at noon"
    assert recent[0]["responded_at"] is not None


def test_dashboard_module_imports_cleanly():
    """Smoke: importing obs.dashboard should not crash."""
    import importlib
    import obs.dashboard as d
    importlib.reload(d)
    for name in ("post_question", "_post_question_async", "fire_respond_subprocess",
                 "_recent_qa", "_recent_qa_async", "_render_chat_form", "_render_recent_qa"):
        assert hasattr(d, name), f"missing: {name}"


def test_fire_respond_subprocess_returns_pid(test_agent):
    """Sync test (no async fixture) — works because no outer event loop."""
    from obs import dashboard

    # Insert a row first via sync wrapper. This works because the surrounding
    # test is NOT an async test — there's no event loop, so asyncio.run is fine.
    inbox_id = dashboard.post_question(test_agent, "trigger respond")
    assert inbox_id > 0

    pid = dashboard.fire_respond_subprocess(test_agent)
    assert isinstance(pid, int) and pid > 0

    # Don't wait — fire-and-forget. The subprocess will exit on its own
    # whenever the LLM call completes (or fails fast against a stubbed env).
    # We only assert the launch succeeded.
