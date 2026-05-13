"""Unit tests for scripts/run_hourly_orchestrator.py.

Pure-function tests for guard + outcome-line formatting are offline.
Async main()/_run_sector/_run_allocator tests stub _run_subprocess_impl so
no real processes spawn; runtime is sub-second.
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
        # Identify skill: last positional arg for run_skill.py / run_scheduled_skill.sh,
        # or the .py filename for run_mike_allocator.py.
        key = None
        if any("run_skill.py" in a for a in args):
            # ['python', '.../run_skill.py', '<sector>', 'review']
            key = args[-2]
        elif any("run_scheduled_skill.sh" in a for a in args):
            key = args[-1]
        elif any("run_mike_allocator.py" in a for a in args):
            key = "mike-allocator"
        calls_log.append({"key": key, "args": args, "timeout_sec": timeout_sec})
        default = {"exit_code": 0, "duration_ms": 100, "timed_out": False}
        result = {**default, **(results_by_skill.get(key, {}))}
        result["args"] = args
        return result

    monkeypatch.setattr(orch, "_run_subprocess_impl", _stub)
    return calls_log


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
    calls = _make_subprocess_stub(orch, monkeypatch, {})

    rc = await orch.main()
    assert rc == 0

    keys = [c["key"] for c in calls]
    # All 11 sectors + allocator + heartbeat in order
    assert set(keys[: len(orch.PIPELINE_SECTORS)]) == set(orch.PIPELINE_SECTORS)
    assert keys[-2] == "mike-allocator"
    assert keys[-1] == "hourly-review"
    assert len(keys) == len(orch.PIPELINE_SECTORS) + 2


async def test_main_continues_when_sectors_fail(orch, monkeypatch):
    """If sectors fail, phase 2+3 must still run."""
    monkeypatch.setattr(orch, "_is_quiet_or_weekend",
                        lambda *a, **kw: (False, False))
    calls = _make_subprocess_stub(orch, monkeypatch, {
        "atlas": {"exit_code": 1, "duration_ms": 50},
        "maya":  {"exit_code": -9, "duration_ms": 900000, "timed_out": True},
    })

    rc = await orch.main()
    assert rc == 0
    keys = [c["key"] for c in calls]
    assert "mike-allocator" in keys
    assert "hourly-review" in keys


async def test_main_continues_when_allocator_times_out(orch, monkeypatch):
    """Allocator timeout doesn't block phase 3 heartbeat."""
    monkeypatch.setattr(orch, "_is_quiet_or_weekend",
                        lambda *a, **kw: (False, False))
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
    calls = _make_subprocess_stub(orch, monkeypatch, {
        "mike-allocator": {"exit_code": 2, "duration_ms": 1200},
    })

    rc = await orch.main()
    assert rc == 0
    keys = [c["key"] for c in calls]
    assert "hourly-review" in keys


async def test_main_passes_correct_timeouts(orch, monkeypatch):
    """Per-skill timeouts must match the constants."""
    monkeypatch.setattr(orch, "_is_quiet_or_weekend",
                        lambda *a, **kw: (False, False))
    calls = _make_subprocess_stub(orch, monkeypatch, {})

    await orch.main()
    by_key = {c["key"]: c for c in calls}
    assert by_key["atlas"]["timeout_sec"] == orch.SKILL_TIMEOUT_SEC
    assert by_key["mike-allocator"]["timeout_sec"] == orch.ALLOCATOR_TIMEOUT_SEC
    assert by_key["hourly-review"]["timeout_sec"] == orch.HEARTBEAT_TIMEOUT_SEC


async def test_main_sector_concurrency_respected(orch, monkeypatch):
    """Concurrency limit (CONCURRENCY=4) must be honored — never >N in flight."""
    monkeypatch.setattr(orch, "_is_quiet_or_weekend",
                        lambda *a, **kw: (False, False))
    monkeypatch.setattr(orch, "CONCURRENCY", 3)

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def _slow_stub(args, log_path, timeout_sec):
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.05)
        finally:
            async with lock:
                in_flight -= 1
        return {"args": args, "exit_code": 0, "duration_ms": 50, "timed_out": False}

    monkeypatch.setattr(orch, "_run_subprocess_impl", _slow_stub)

    await orch.main()
    assert max_in_flight <= 3


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
