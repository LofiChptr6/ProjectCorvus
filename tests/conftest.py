"""Shared pytest fixtures.

Tests run against the LIVE postgres database (config.yaml or PG_* env vars).
Each fixture that writes data uses an isolation prefix (`__test_<suffix>__`) for
agent_name so cleanup is trivial — the post-test fixture deletes every row whose
agent_name starts with the prefix.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Ensure repo root is importable regardless of where pytest is invoked from.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Tests should NEVER place real orders, send Telegrams, or hit IBKR live data.
os.environ.setdefault("TRADING_TEST_MODE", "1")

TEST_AGENT_PREFIX = "__test_"


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _init_schema():
    """Apply schema once per test session."""
    from db.schema import init_db, close_pool
    await init_db()
    yield
    await close_pool()


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    """Default-deny real Telegram sends from any test. Tests that want to
    verify the call install their own recorder over this."""
    async def _noop_message(*a, **kw): return {"ok": True}
    async def _noop_photo(*a, **kw): return {"ok": True}
    import approval.telegram
    monkeypatch.setattr(approval.telegram, "send_message", _noop_message, raising=False)
    monkeypatch.setattr(approval.telegram, "send_photo", _noop_photo, raising=False)


@pytest_asyncio.fixture
async def test_agent():
    """Return a unique test-agent name; clean up rows after the test."""
    import uuid
    name = f"{TEST_AGENT_PREFIX}{uuid.uuid4().hex[:8]}__"
    yield name
    # Cleanup: nuke every row this test inserted under this agent name.
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Add to this list as more tables get test data.
        for table in (
            "agent_inbox",
            "agent_thesis",
            "agent_conviction", "agent_conviction_shadow",
            "agent_forecast", "agent_forecast_shadow",
            "agent_evening_digests",
            "sector_story",
        ):
            try:
                await conn.execute(f"DELETE FROM {table} WHERE agent_name=$1", name)
            except Exception:
                # Table might not exist in some test runs — ignore.
                pass
