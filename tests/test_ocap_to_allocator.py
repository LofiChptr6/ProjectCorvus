"""Verify the OCAP→allocator wiring (Phase 1b):

1. After a successful `ocap_triggered_review` job, the worker enqueues an
   `ocap_rebalance` job (priority=5, coalesce_key='ocap:rebalance', 60s window).
2. Multiple OCAP completions within 60s coalesce into one rebalance job.
3. The advisory lock in run_mike_allocator.main() prevents concurrent runs:
   if another process holds the lock, main() returns 2 immediately.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_runner():
    """Load scripts/run_queue_worker.py as a fresh module (it's a script,
    not a package member)."""
    path = _REPO_ROOT / "scripts" / "run_queue_worker.py"
    spec = importlib.util.spec_from_file_location("run_queue_worker", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_allocator():
    path = _REPO_ROOT / "scripts" / "run_mike_allocator.py"
    spec = importlib.util.spec_from_file_location("run_mike_allocator", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def worker():
    return _load_runner()


@pytest.fixture
def allocator():
    return _load_allocator()


@pytest_asyncio.fixture
async def _job_cleanup():
    """Track agent_job ids inserted by the test for cleanup. Tests append the
    ids they care about; the fixture deletes them after the test."""
    ids: list[int] = []
    yield ids
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        if ids:
            await conn.execute(
                "DELETE FROM agent_job WHERE id = ANY($1::bigint[])",
                ids,
            )


@pytest_asyncio.fixture
async def _ocap_rebalance_cleanup():
    """Cleanup any ocap_rebalance jobs created during the test."""
    yield
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_job WHERE coalesce_key='ocap:rebalance'"
        )


async def _enqueue_ocap_review_job(agent: str, symbol: str = "SPY") -> int:
    """Insert one ocap_triggered_review job and return its id."""
    from db import store
    res = await store.enqueue_job_coalesced(
        agent_name=agent,
        job_type="ocap_triggered_review",
        payload={"symbol": symbol, "triggers": ["rolling_std_breach"]},
        priority=5,
        coalesce_key=f"test:ocap:{agent}:{symbol}",
        coalesce_window_s=10,
        triggers_seen=["rolling_std_breach"],
    )
    return res["job_id"]


# ── Step 3b: enqueue ocap_rebalance after success ─────────────────────────────


def _fake_picked_job(test_agent: str, job_id: int, *, symbol: str = "SPY"):
    """Build a job dict matching pick_next_job's shape so we can inject it
    deterministically — the live system's queue may have higher-priority
    rows that would otherwise be picked first."""
    return {
        "id": job_id,
        "agent_name": test_agent,
        "job_type": "ocap_triggered_review",
        "priority": 5,
        "payload": {"symbol": symbol, "triggers": ["rolling_std_breach"]},
        "triggers_seen": ["rolling_std_breach"],
    }


async def test_successful_ocap_review_enqueues_rebalance(
    worker, test_agent, _job_cleanup, _ocap_rebalance_cleanup, monkeypatch,
):
    """After an ocap_triggered_review job completes with exit=0 AND at least
    one material conviction change, the worker MUST call
    enqueue_job_coalesced with the right (job_type, key, payload).

    Approach: mock enqueue_job_coalesced to capture the call. We don't poll
    the live `agent_job` table because the production system may have
    in-flight ocap_rebalance rows that coalesce my call, hiding it from a
    naive WHERE source_job_id = ... query."""
    job_id = await _enqueue_ocap_review_job(test_agent)
    _job_cleanup.append(job_id)

    import db.store as store
    async def _fake_pick(worker_id):
        return _fake_picked_job(test_agent, job_id)
    monkeypatch.setattr(store, "pick_next_job", _fake_pick)

    async def _fake_dispatch(job):
        return 0, {
            "status": "ok", "session_id": "fake-session",
            "write_summary": {"convictions_materially_changed": 1},
        }
    monkeypatch.setattr(worker, "_dispatch", _fake_dispatch)

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(worker, "_write_ocap_inbox_context", _noop)
    monkeypatch.setattr(store, "mark_job_done", _noop)
    monkeypatch.setattr(store, "set_job_skill_result", _noop)

    captured: list[dict] = []
    async def _fake_enqueue(**kwargs):
        captured.append(kwargs)
        return {"action": "enqueued", "job_id": 999_999}
    monkeypatch.setattr(store, "enqueue_job_coalesced", _fake_enqueue)

    rc = await worker._one_iteration("test-worker")
    assert rc == "done"

    assert len(captured) == 1, (
        f"expected exactly one enqueue_job_coalesced call, got {len(captured)}"
    )
    call = captured[0]
    assert call["agent_name"] == "mike"
    assert call["job_type"] == "ocap_rebalance"
    assert call["priority"] == 5
    assert call["coalesce_key"] == "ocap:rebalance"
    assert call["coalesce_window_s"] == 60
    payload = call["payload"]
    assert payload["trigger"] == "ocap_review_completed"
    assert payload["source_job_id"] == job_id
    assert payload["source_agent"] == test_agent
    assert payload["source_symbol"] == "SPY"
    assert payload["convictions_materially_changed"] == 1


async def test_immaterial_ocap_review_skips_rebalance_enqueue(
    worker, test_agent, _job_cleanup, _ocap_rebalance_cleanup, monkeypatch,
):
    """Materiality gate: OCAP review that produced zero material changes
    MUST NOT enqueue an ocap_rebalance — avoids no-op allocator runs."""
    job_id = await _enqueue_ocap_review_job(test_agent, symbol="EFA")
    _job_cleanup.append(job_id)

    import db.store as store
    async def _fake_pick(worker_id):
        return _fake_picked_job(test_agent, job_id, symbol="EFA")
    monkeypatch.setattr(store, "pick_next_job", _fake_pick)

    # Rollup explicitly reports zero material changes.
    async def _fake_dispatch(job):
        return 0, {
            "status": "ok", "session_id": "fake-session",
            "write_summary": {"convictions_materially_changed": 0},
        }

    monkeypatch.setattr(worker, "_dispatch", _fake_dispatch)

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(worker, "_write_ocap_inbox_context", _noop)
    monkeypatch.setattr(store, "mark_job_done", _noop)
    monkeypatch.setattr(store, "set_job_skill_result", _noop)

    # Capture enqueue calls instead of polling the DB — live OCAP traffic
    # would coalesce my rebalance onto an existing row and a naive query
    # by source_job_id would miss it.
    captured: list[dict] = []
    async def _fake_enqueue(**kwargs):
        captured.append(kwargs)
        return {"action": "enqueued", "job_id": 0}
    monkeypatch.setattr(store, "enqueue_job_coalesced", _fake_enqueue)

    rc = await worker._one_iteration("test-worker")
    assert rc == "done"
    assert captured == [], (
        f"materiality gate should have suppressed enqueue, got {captured}"
    )


async def test_missing_write_summary_skips_rebalance(
    worker, test_agent, _job_cleanup, _ocap_rebalance_cleanup, monkeypatch,
):
    """If the runner didn't emit convictions_materially_changed (e.g. legacy
    rollup), treat as zero — don't fire spurious rebalances."""
    job_id = await _enqueue_ocap_review_job(test_agent, symbol="GLD")
    _job_cleanup.append(job_id)

    import db.store as store
    async def _fake_pick(worker_id):
        return _fake_picked_job(test_agent, job_id, symbol="GLD")
    monkeypatch.setattr(store, "pick_next_job", _fake_pick)

    async def _fake_dispatch(job):
        # Old-shape rollup with no write_summary.
        return 0, {"status": "ok", "session_id": "fake-session"}
    monkeypatch.setattr(worker, "_dispatch", _fake_dispatch)
    async def _noop(*a, **kw): return None
    monkeypatch.setattr(worker, "_write_ocap_inbox_context", _noop)
    monkeypatch.setattr(store, "mark_job_done", _noop)
    monkeypatch.setattr(store, "set_job_skill_result", _noop)

    captured: list[dict] = []
    async def _fake_enqueue(**kwargs):
        captured.append(kwargs)
        return {"action": "enqueued", "job_id": 0}
    monkeypatch.setattr(store, "enqueue_job_coalesced", _fake_enqueue)

    rc = await worker._one_iteration("test-worker")
    assert rc == "done"
    assert captured == []


async def test_non_ocap_jobs_do_not_enqueue_rebalance(
    worker, test_agent, _job_cleanup, _ocap_rebalance_cleanup, monkeypatch,
):
    """A completed ticker_review (not OCAP-fired) must NOT trigger rebalance."""
    from db import store
    res = await store.enqueue_job_coalesced(
        agent_name=test_agent,
        job_type="ticker_review",
        payload={"symbol": "SPY"},
        priority=10,
        coalesce_key=f"test:tr:{test_agent}",
        coalesce_window_s=60,
    )
    _job_cleanup.append(res["job_id"])

    async def _fake_pick(worker_id):
        return {
            "id": res["job_id"],
            "agent_name": test_agent,
            "job_type": "ticker_review",
            "priority": 10,
            "payload": {"symbol": "SPY"},
            "triggers_seen": None,
        }
    monkeypatch.setattr(store, "pick_next_job", _fake_pick)

    async def _fake_dispatch(job):
        return 0, {"status": "ok"}
    monkeypatch.setattr(worker, "_dispatch", _fake_dispatch)

    async def _noop(*a, **kw): return None
    monkeypatch.setattr(store, "mark_job_done", _noop)
    monkeypatch.setattr(store, "set_job_skill_result", _noop)

    captured: list[dict] = []
    async def _fake_enqueue(**kwargs):
        captured.append(kwargs)
        return {"action": "enqueued", "job_id": 0}
    monkeypatch.setattr(store, "enqueue_job_coalesced", _fake_enqueue)

    rc = await worker._one_iteration("test-worker")
    assert rc == "done"
    assert captured == [], "ticker_review wrongly triggered ocap_rebalance enqueue"


# ── Coalescing within window ───────────────────────────────────────────────────


async def test_multiple_ocap_completions_coalesce_to_one_rebalance(
    worker, test_agent, _job_cleanup, _ocap_rebalance_cleanup, monkeypatch,
):
    """Five ocap_triggered_review completions in the same 60s window must
    each hand the SAME coalesce_key + window to the queue layer. The actual
    1-row-out-of-5 collapsing happens inside enqueue_job_coalesced (tested
    separately); here we verify the WORKER passes the right inputs to the
    coalescer."""
    async def _fake_dispatch(job):
        return 0, {
            "status": "ok",
            "write_summary": {"convictions_materially_changed": 1},
        }
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(worker, "_dispatch", _fake_dispatch)
    monkeypatch.setattr(worker, "_write_ocap_inbox_context", _noop)
    import db.store as store
    monkeypatch.setattr(store, "mark_job_done", _noop)
    monkeypatch.setattr(store, "set_job_skill_result", _noop)

    captured_calls: list[dict] = []
    async def _fake_enqueue(**kwargs):
        captured_calls.append(kwargs)
        # Mimic real coalescing: first call enqueues, rest coalesce.
        if len(captured_calls) == 1:
            return {"action": "enqueued", "job_id": 1000}
        return {"action": "coalesced", "job_id": 1000}
    monkeypatch.setattr(store, "enqueue_job_coalesced", _fake_enqueue)

    # Enqueue 5 distinct ocap_triggered_review jobs.
    inserted_ids: list[int] = []
    for i in range(5):
        from db.schema import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO agent_job
                     (agent_name, job_type, priority, payload, coalesce_key, triggers_seen)
                   VALUES ($1, 'ocap_triggered_review', 5, $2::jsonb,
                           $3, $4::jsonb)
                   RETURNING id""",
                test_agent,
                json.dumps({"symbol": f"SYM{i}", "triggers": ["rolling_std_breach"]}),
                f"test:multi:{test_agent}:{i}",
                json.dumps(["rolling_std_breach"]),
            )
            inserted_ids.append(row["id"])
            _job_cleanup.append(row["id"])

    # Mocked pick_next_job feeds the test's rows one at a time so the
    # worker doesn't compete with the live queue.
    pick_iter = iter(inserted_ids)
    async def _fake_pick(worker_id):
        nxt = next(pick_iter, None)
        if nxt is None:
            return None
        i = inserted_ids.index(nxt)
        return _fake_picked_job(test_agent, nxt, symbol=f"SYM{i}")
    monkeypatch.setattr(store, "pick_next_job", _fake_pick)

    # Drain all 5 jobs.
    for _ in range(5):
        rc = await worker._one_iteration("test-worker")
        assert rc == "done"

    # All 5 review completions called enqueue_job_coalesced with the same
    # key+window — the coalescer (separately tested) collapses them.
    assert len(captured_calls) == 5
    keys = {c["coalesce_key"] for c in captured_calls}
    windows = {c["coalesce_window_s"] for c in captured_calls}
    assert keys == {"ocap:rebalance"}
    assert windows == {60}


# ── Advisory lock blocks concurrent allocator runs ────────────────────────────


async def test_allocator_advisory_lock_blocks_concurrent_run(allocator):
    """If another connection holds pg_advisory_lock(ALLOCATOR_LOCK_KEY),
    main() must return 2 immediately without doing any work."""
    from db.schema import get_pool

    pool = await get_pool()
    holder_conn = await pool.acquire()
    try:
        # Acquire the lock on a holder connection.
        await holder_conn.fetchval(
            "SELECT pg_advisory_lock($1)", allocator.ALLOCATOR_LOCK_KEY,
        )

        # Now main() should see the lock taken and return 2 immediately.
        # Even though _main_locked would crash without a properly stubbed
        # mcp_server/IBKR connection, we never reach _main_locked because
        # the lock guard short-circuits before it.
        rc = await allocator.main()
        assert rc == 2
    finally:
        # Release the lock from the holder connection.
        await holder_conn.execute(
            "SELECT pg_advisory_unlock($1)", allocator.ALLOCATOR_LOCK_KEY,
        )
        await pool.release(holder_conn)


async def test_allocator_releases_lock_after_run(allocator, monkeypatch):
    """After main() returns, the lock must be released so a subsequent
    call can acquire it. Stub _main_locked so we don't actually rebalance."""
    async def _fake_locked():
        return 0
    monkeypatch.setattr(allocator, "_main_locked", _fake_locked)

    rc1 = await allocator.main()
    assert rc1 == 0
    # Second call should also succeed — lock got released.
    rc2 = await allocator.main()
    assert rc2 == 0
