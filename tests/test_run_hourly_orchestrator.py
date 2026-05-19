"""Unit tests for scripts/run_hourly_orchestrator.py.

Pure-function tests for guard + outcome-line formatting are offline.
Async main() tests stub both `_run_subprocess_impl` (allocator + heartbeat
still subprocess) and `_prime_queue_for_agent` (the queue-prime path that
replaced the per-sector subprocess fan-out), so no real processes spawn
and no DB writes happen; runtime is sub-second.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Override the conftest's session-scoped autouse _init_schema fixture: this
# module mocks every boundary, so we don't need a live DB. Avoids a pre-existing
# UniqueViolationError on agent_forecast seed data in production schemas.
@pytest_asyncio.fixture(scope="session", autouse=True)
async def _init_schema():
    yield


def _load_orchestrator():
    path = _REPO_ROOT / "scripts" / "run_hourly_orchestrator.py"
    spec = importlib.util.spec_from_file_location("run_hourly_orchestrator", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def orch():
    return _load_orchestrator()


AZ = ZoneInfo("America/Phoenix")


# ─────────────────────────────────────────────────────────────────────────────
# _is_quiet_or_weekend — pure
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hour,expected_quiet", [
    (22, True), (23, True), (0, True), (3, True), (4, True),
    (5, False), (6, False), (10, False), (15, False), (21, False),
])
def test_quiet_window_hours(orch, hour, expected_quiet):
    # Tue 2026-05-12 is a Tuesday (weekday) — isolate the hour dimension.
    fake_now = datetime(2026, 5, 12, hour, 30, 0, tzinfo=AZ)
    is_quiet, is_weekend = orch._is_quiet_or_weekend(fake_now)
    assert is_quiet == expected_quiet
    assert is_weekend is False


def test_weekend_saturday(orch):
    fake_now = datetime(2026, 5, 9, 10, 30, 0, tzinfo=AZ)  # Sat
    is_quiet, is_weekend = orch._is_quiet_or_weekend(fake_now)
    assert is_quiet is False
    assert is_weekend is True


def test_weekend_sunday(orch):
    fake_now = datetime(2026, 5, 10, 10, 30, 0, tzinfo=AZ)  # Sun
    is_quiet, is_weekend = orch._is_quiet_or_weekend(fake_now)
    assert is_weekend is True


def test_weekday_open_hours(orch):
    fake_now = datetime(2026, 5, 12, 10, 30, 0, tzinfo=AZ)  # Tue
    is_quiet, is_weekend = orch._is_quiet_or_weekend(fake_now)
    assert is_quiet is False
    assert is_weekend is False


# ─────────────────────────────────────────────────────────────────────────────
# _phase_outcome_line — pure
# ─────────────────────────────────────────────────────────────────────────────

def test_outcome_line_ok(orch):
    line = orch._phase_outcome_line("phase 2", "mike-allocator",
                                    {"exit_code": 0, "duration_ms": 123, "timed_out": False})
    assert "phase 2: mike-allocator ok (123ms)" == line


def test_outcome_line_timeout(orch):
    line = orch._phase_outcome_line("phase 1a/maya", "maya",
                                    {"exit_code": -9, "duration_ms": 900000, "timed_out": True})
    assert "TIMED OUT" in line and "900000ms" in line


def test_outcome_line_allocator_guard_skip(orch):
    """exit=2 from mike-allocator is a guard skip, not a failure."""
    line = orch._phase_outcome_line("phase 2", "mike-allocator",
                                    {"exit_code": 2, "duration_ms": 800, "timed_out": False})
    assert "skipped by guard" in line
    assert "exit=2" in line


def test_outcome_line_other_failure(orch):
    line = orch._phase_outcome_line("phase 3", "hourly-review",
                                    {"exit_code": 1, "duration_ms": 50, "timed_out": False})
    assert "exit=1" in line and "TIMED OUT" not in line and "ok" not in line


def test_outcome_line_other_skill_exit_2_is_failure(orch):
    """exit=2 is only special-cased for mike-allocator (guard skip semantic)."""
    line = orch._phase_outcome_line("phase 1a/atlas", "atlas",
                                    {"exit_code": 2, "duration_ms": 50, "timed_out": False})
    assert "skipped by guard" not in line
    assert "exit=2" in line


# ─────────────────────────────────────────────────────────────────────────────
# Stub _run_subprocess_impl so async tests don't spawn real processes
# ─────────────────────────────────────────────────────────────────────────────

def _make_subprocess_stub(orch, monkeypatch, results_by_skill: dict[str, dict]):
    """Install a stub that returns a fake subprocess result keyed by skill/agent name.

    Inspects argv to identify which skill/sector is being invoked, and returns
    the matching entry from results_by_skill (default to ok if not specified).
    Records every call on calls_log.
    """
    calls_log: list[dict] = []

    async def _stub(args: list[str], log_path, timeout_sec: int):
        # Identify skill from argv shape.
        key = None
        if any("run_skill.py" in a for a in args):
            # ['python', '.../run_skill.py', '<sector>', 'review']
            key = args[-2]
        elif any("run_scheduled_skill.sh" in a for a in args):
            key = args[-1]
        elif any("run_mike_allocator.py" in a for a in args):
            key = "mike-allocator"
        elif any("run_hourly_review.py" in a for a in args):
            key = "hourly-review"
        calls_log.append({"key": key, "args": args, "timeout_sec": timeout_sec})
        default = {"exit_code": 0, "duration_ms": 100, "timed_out": False}
        result = {**default, **(results_by_skill.get(key, {}))}
        result["args"] = args
        return result

    monkeypatch.setattr(orch, "_run_subprocess_impl", _stub)
    return calls_log


def _stub_queue_prime(orch, monkeypatch, watchlist_size: int = 30,
                      exception_for: set[str] | None = None):
    """Replace `prime_agent_queue` so we don't need a live DB. Returns the
    list of agents the stub was called for so tests can assert fan-out."""
    calls: list[str] = []

    async def _stub(agent: str) -> dict:
        calls.append(agent)
        if exception_for and agent in exception_for:
            raise RuntimeError(f"forced for {agent}")
        return {
            "agent": agent,
            "sector_summary_enqueued": 1,
            "sector_summary_coalesced": 0,
            "ticker_review_enqueued": watchlist_size,
            "ticker_review_coalesced": 0,
            "watchlist_size": watchlist_size,
        }

    import meta_agent.queue_primer as _primer
    monkeypatch.setattr(_primer, "prime_agent_queue", _stub)
    # Also stub the queue-stats heartbeat at the end of phase 1a.
    async def _fake_stats():
        return {"queued": watchlist_size * len(orch.PIPELINE_SECTORS), "running": 0,
                "done_lifetime": 0, "failed_lifetime": 0, "skipped_lifetime": 0,
                "oldest_queued_at": None}
    import db.store as _store
    monkeypatch.setattr(_store, "get_queue_stats", _fake_stats)
    return calls


# ─────────────────────────────────────────────────────────────────────────────
# main() guard path — heartbeat only
# ─────────────────────────────────────────────────────────────────────────────

async def test_main_quiet_window_runs_heartbeat_only(orch, monkeypatch):
    monkeypatch.setattr(orch, "_is_quiet_or_weekend",
                        lambda *a, **kw: (True, False))
    calls = _make_subprocess_stub(orch, monkeypatch, {})

    rc = await orch.main()
    assert rc == 0
    keys = [c["key"] for c in calls]
    assert keys == ["hourly-review"]


async def test_main_weekend_runs_heartbeat_only(orch, monkeypatch):
    monkeypatch.setattr(orch, "_is_quiet_or_weekend",
                        lambda *a, **kw: (False, True))
    calls = _make_subprocess_stub(orch, monkeypatch, {})
    rc = await orch.main()
    assert rc == 0
    assert [c["key"] for c in calls] == ["hourly-review"]


# ─────────────────────────────────────────────────────────────────────────────
# main() full pipeline
# ─────────────────────────────────────────────────────────────────────────────

async def test_main_full_pipeline_happy_path(orch, monkeypatch):
    monkeypatch.setattr(orch, "_is_quiet_or_weekend",
                        lambda *a, **kw: (False, False))
    primed = _stub_queue_prime(orch, monkeypatch)
    sub_calls = _make_subprocess_stub(orch, monkeypatch, {})

    rc = await orch.main()
    assert rc == 0

    # Phase 1a: queue primed for every sector
    assert set(primed) == set(orch.PIPELINE_SECTORS)
    # Phase 2 + 3: allocator + heartbeat subprocesses still run
    sub_keys = [c["key"] for c in sub_calls]
    assert sub_keys == ["mike-allocator", "hourly-review"]


async def test_main_continues_when_a_sector_prime_fails(orch, monkeypatch):
    """If one agent's queue prime raises, the rest still prime and phase 2+3 run."""
    monkeypatch.setattr(orch, "_is_quiet_or_weekend",
                        lambda *a, **kw: (False, False))
    primed = _stub_queue_prime(orch, monkeypatch, exception_for={"atlas", "maya"})
    sub_calls = _make_subprocess_stub(orch, monkeypatch, {})

    rc = await orch.main()
    assert rc == 0
    # Every agent was attempted (asyncio.gather with return_exceptions=True).
    assert set(primed) == set(orch.PIPELINE_SECTORS)
    sub_keys = [c["key"] for c in sub_calls]
    assert sub_keys == ["mike-allocator", "hourly-review"]


async def test_main_continues_when_allocator_times_out(orch, monkeypatch):
    """Allocator timeout doesn't block phase 3 heartbeat."""
    monkeypatch.setattr(orch, "_is_quiet_or_weekend",
                        lambda *a, **kw: (False, False))
    _stub_queue_prime(orch, monkeypatch)
    calls = _make_subprocess_stub(orch, monkeypatch, {
        "mike-allocator": {"exit_code": -9, "duration_ms": 180000, "timed_out": True},
    })

    rc = await orch.main()
    assert rc == 0
    keys = [c["key"] for c in calls]
    assert "hourly-review" in keys


async def test_main_continues_when_allocator_guard_skip(orch, monkeypatch):
    """Allocator exit=2 (guard skip) is normal; heartbeat still runs."""
    monkeypatch.setattr(orch, "_is_quiet_or_weekend",
                        lambda *a, **kw: (False, False))
    _stub_queue_prime(orch, monkeypatch)
    calls = _make_subprocess_stub(orch, monkeypatch, {
        "mike-allocator": {"exit_code": 2, "duration_ms": 1200},
    })

    rc = await orch.main()
    assert rc == 0
    keys = [c["key"] for c in calls]
    assert "hourly-review" in keys


async def test_main_passes_correct_timeouts(orch, monkeypatch):
    """Allocator + heartbeat timeouts must still match the constants."""
    monkeypatch.setattr(orch, "_is_quiet_or_weekend",
                        lambda *a, **kw: (False, False))
    _stub_queue_prime(orch, monkeypatch)
    calls = _make_subprocess_stub(orch, monkeypatch, {})

    await orch.main()
    by_key = {c["key"]: c for c in calls}
    assert by_key["mike-allocator"]["timeout_sec"] == orch.ALLOCATOR_TIMEOUT_SEC
    assert by_key["hourly-review"]["timeout_sec"] == orch.HEARTBEAT_TIMEOUT_SEC


# ─────────────────────────────────────────────────────────────────────────────
# _run_subprocess — light integration test (real process, fast)
# ─────────────────────────────────────────────────────────────────────────────

async def test_run_subprocess_captures_exit_code_ok(orch, tmp_path):
    log_path = tmp_path / "out.log"
    r = await orch._run_subprocess(["/bin/true"], log_path, timeout_sec=5)
    assert r["exit_code"] == 0
    assert r["timed_out"] is False
    assert r["duration_ms"] >= 0


async def test_run_subprocess_captures_non_zero(orch, tmp_path):
    log_path = tmp_path / "out.log"
    r = await orch._run_subprocess(["/bin/false"], log_path, timeout_sec=5)
    assert r["exit_code"] == 1
    assert r["timed_out"] is False


async def test_run_subprocess_handles_timeout(orch, tmp_path):
    log_path = tmp_path / "out.log"
    r = await orch._run_subprocess(["/bin/sleep", "10"], log_path, timeout_sec=1)
    assert r["timed_out"] is True
    assert r["exit_code"] == -9


async def test_run_subprocess_handles_missing_executable(orch, tmp_path):
    log_path = tmp_path / "out.log"
    r = await orch._run_subprocess(
        ["/nonexistent/path/to/binary"], log_path, timeout_sec=5,
    )
    assert r["exit_code"] == 127
    assert r["timed_out"] is False


async def test_run_subprocess_writes_stdout_to_log(orch, tmp_path):
    log_path = tmp_path / "out.log"
    await orch._run_subprocess(["/bin/sh", "-c", "echo hello-from-child"],
                               log_path, timeout_sec=5)
    body = log_path.read_text()
    assert "hello-from-child" in body
