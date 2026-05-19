"""Verify that db.store.get_active_convictions skips rows from agents whose
per-agent kill switch is active, while still returning rows from non-killed
agents. This preserves the per-agent-kill semantic ('don't trade based on this
agent's view') after the kill check was removed from the queue worker.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest_asyncio.fixture
async def _kill_cleanup():
    """Track agent names whose kill_switch rows need cleanup."""
    inserted: list[str] = []
    yield inserted
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        for name in inserted:
            await conn.execute(
                "DELETE FROM kill_switch WHERE agent_name=$1", name,
            )


async def _seed_conviction(agent_name: str, symbol: str, conviction: float = 1.0,
                            expires_at: datetime | None = None):
    from db.schema import get_pool
    pool = await get_pool()
    if expires_at is None:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=2)
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_conviction
                  (agent_name, symbol, direction, conviction, expires_at)
               VALUES ($1, $2, 'long', $3, $4)
               ON CONFLICT (agent_name, symbol) DO UPDATE
                  SET conviction=EXCLUDED.conviction, expires_at=EXCLUDED.expires_at""",
            agent_name, symbol, conviction, expires_at,
        )


async def test_get_active_convictions_includes_non_killed_agents(test_agent):
    """Baseline: a conviction from an agent with no kill switch is returned."""
    import db.store as store
    await _seed_conviction(test_agent, "SPY", conviction=0.7)
    rows = await store.get_active_convictions()
    assert any(r["agent_name"] == test_agent and r["symbol"] == "SPY" for r in rows)


async def test_get_active_convictions_excludes_killed_agents(test_agent, _kill_cleanup):
    """Activating per-agent kill must hide that agent's convictions."""
    import db.store as store
    await _seed_conviction(test_agent, "QQQ", conviction=0.5)
    await store.set_kill_switch(active=True, agent_name=test_agent, activated_by="test")
    _kill_cleanup.append(test_agent)

    rows = await store.get_active_convictions()
    assert not any(r["agent_name"] == test_agent for r in rows), (
        "killed agent's conviction leaked into get_active_convictions"
    )


async def test_get_active_convictions_re_includes_after_unkill(test_agent, _kill_cleanup):
    """Deactivating kill must restore visibility (latest-wins semantics)."""
    import db.store as store
    await _seed_conviction(test_agent, "IWM", conviction=0.4)
    await store.set_kill_switch(active=True, agent_name=test_agent, activated_by="test")
    _kill_cleanup.append(test_agent)

    rows_killed = await store.get_active_convictions()
    assert not any(r["agent_name"] == test_agent for r in rows_killed)

    await store.set_kill_switch(active=False, agent_name=test_agent)
    rows_alive = await store.get_active_convictions()
    assert any(r["agent_name"] == test_agent for r in rows_alive), (
        "lifting kill switch did not restore agent's conviction visibility"
    )


async def test_get_active_convictions_isolated_per_agent(_kill_cleanup):
    """Killing agent A must NOT hide agent B's convictions."""
    import db.store as store
    a = f"__test_killf_a_{uuid.uuid4().hex[:6]}__"
    b = f"__test_killf_b_{uuid.uuid4().hex[:6]}__"
    await _seed_conviction(a, "DIA", conviction=0.6)
    await _seed_conviction(b, "DIA", conviction=0.3)
    await store.set_kill_switch(active=True, agent_name=a, activated_by="test")
    _kill_cleanup.append(a)
    _kill_cleanup.append(b)

    rows = await store.get_active_convictions()
    a_visible = any(r["agent_name"] == a for r in rows)
    b_visible = any(r["agent_name"] == b for r in rows)
    assert not a_visible, "killed agent A should be filtered"
    assert b_visible, "non-killed agent B was incorrectly filtered"

    # cleanup conviction rows directly (test_agent fixture not used here)
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_conviction WHERE agent_name IN ($1, $2)", a, b,
        )


async def test_worker_no_longer_checks_kill():
    """Regression: the worker's _one_iteration must not call is_killed.
    This test is a source-level check so it fails loudly if the kill block
    is ever re-introduced without a separate design decision."""
    src = (_REPO_ROOT / "scripts" / "run_queue_worker.py").read_text()
    # Function body of _one_iteration. We tolerate is_killed appearing in a
    # docstring / comment but not in an executable call.
    import re
    # Strip comments + docstrings before searching for executable calls.
    code_only = re.sub(r'#[^\n]*', '', src)
    code_only = re.sub(r'"""[\s\S]*?"""', '', code_only)
    code_only = re.sub(r"'''[\s\S]*?'''", '', code_only)
    assert "store.is_killed" not in code_only, (
        "scripts/run_queue_worker.py still calls store.is_killed in executable code. "
        "Kill switch enforcement is intentionally moved out of the worker — to the "
        "allocator (run_mike_allocator._guard_skip) and the order layer "
        "(risk/checks/kill_switch.py + place_order.py)."
    )
