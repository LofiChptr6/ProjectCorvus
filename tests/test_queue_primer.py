"""Unit tests for meta_agent/queue_primer.py.

Stubs `db.store.enqueue_job_coalesced` and `db.store.load_agent_watchlist`
so no live DB is needed. Tests verify the fan-out shape, exception isolation
across agents, and totals math.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from meta_agent import queue_primer


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _init_schema():
    yield


def _stub_store(monkeypatch, watchlist: dict[str, list[str]],
                enqueue_action: str = "enqueued",
                raise_for: set[str] | None = None):
    """Stub db.store boundaries. `enqueue_action` controls whether each call
    reports as enqueued or coalesced. `raise_for` forces failure for given
    agents (the load_agent_watchlist call raises)."""
    import db.store as _store

    async def _enqueue(**kwargs):
        return {"action": enqueue_action, "job_id": 1}

    async def _watchlist(agent: str):
        if raise_for and agent in raise_for:
            raise RuntimeError(f"forced failure for {agent}")
        return [{"symbol": s} for s in watchlist.get(agent, [])]

    monkeypatch.setattr(_store, "enqueue_job_coalesced", _enqueue)
    monkeypatch.setattr(_store, "load_agent_watchlist", _watchlist)


async def test_prime_agent_queue_basic_shape(monkeypatch):
    _stub_store(monkeypatch, {"atlas": ["SPY", "QQQ", "TLT"]})
    out = await queue_primer.prime_agent_queue("atlas")
    assert out["agent"] == "atlas"
    assert out["sector_summary_enqueued"] == 1
    assert out["sector_summary_coalesced"] == 0
    assert out["ticker_review_enqueued"] == 3
    assert out["ticker_review_coalesced"] == 0
    assert out["watchlist_size"] == 3


async def test_prime_agent_queue_all_coalesced(monkeypatch):
    _stub_store(monkeypatch, {"atlas": ["SPY", "QQQ"]}, enqueue_action="coalesced")
    out = await queue_primer.prime_agent_queue("atlas")
    assert out["sector_summary_enqueued"] == 0
    assert out["sector_summary_coalesced"] == 1
    assert out["ticker_review_enqueued"] == 0
    assert out["ticker_review_coalesced"] == 2


async def test_prime_all_agent_queues_fan_out(monkeypatch):
    wl = {a: ["X", "Y", "Z"] for a in queue_primer.PIPELINE_SECTORS}
    _stub_store(monkeypatch, wl)
    out = await queue_primer.prime_all_agent_queues()
    # 1 sector_summary + 3 ticker_review per agent = 4 per agent
    expected_per_agent = 4
    n_agents = len(queue_primer.PIPELINE_SECTORS)
    assert out["total_enqueued"] == expected_per_agent * n_agents
    assert out["total_coalesced"] == 0
    assert out["failed_agents"] == []
    assert len(out["per_agent"]) == n_agents
    assert {row["agent"] for row in out["per_agent"]} == set(queue_primer.PIPELINE_SECTORS)
    assert all(row["enqueued"] == expected_per_agent for row in out["per_agent"])


async def test_prime_all_isolates_failures(monkeypatch):
    """One agent failing must not poison the rest."""
    wl = {a: ["X", "Y"] for a in queue_primer.PIPELINE_SECTORS}
    _stub_store(monkeypatch, wl, raise_for={"maya", "fab"})
    out = await queue_primer.prime_all_agent_queues()
    assert set(out["failed_agents"]) == {"maya", "fab"}
    # 11 - 2 healthy agents × 3 enqueued each = 27
    healthy = len(queue_primer.PIPELINE_SECTORS) - 2
    assert out["total_enqueued"] == 3 * healthy
    error_rows = [r for r in out["per_agent"] if "error" in r]
    assert {r["agent"] for r in error_rows} == {"maya", "fab"}
    assert all("RuntimeError" in r["error"] for r in error_rows)


async def test_prime_all_empty_watchlists(monkeypatch):
    """Agent with empty watchlist still enqueues 1 sector_summary."""
    wl = {a: [] for a in queue_primer.PIPELINE_SECTORS}
    _stub_store(monkeypatch, wl)
    out = await queue_primer.prime_all_agent_queues()
    n_agents = len(queue_primer.PIPELINE_SECTORS)
    assert out["total_enqueued"] == n_agents  # just the sector_summary per agent
    assert all(row["watchlist_size"] == 0 for row in out["per_agent"])


def test_pipeline_sectors_is_11_sectors():
    """Lock the sector list. Adding a new sector should be a deliberate edit."""
    assert queue_primer.PIPELINE_SECTORS == [
        "atlas", "commodity", "energy", "fab", "fabless", "iron",
        "maya", "rex", "trump", "vera", "volt",
    ]
