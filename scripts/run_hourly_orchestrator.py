#!/usr/bin/env python3
"""Hourly orchestrator — primes the LLM-task queue, runs mike-allocator + heartbeat.

Triggered every hour on the hour by trading-hourly-review.timer (user systemd).
The AZ quiet window (22:00–05:00 MST) and weekends gate phases 1+2 internally;
phase 3 always runs.

Phases:
  0  guard       — AZ quiet or weekend → run phase 3 only
  1a queue prime — INSERT (1 sector_summary + N ticker_review) jobs per agent
                   into agent_job; workers (trading-queue-worker@*.service)
                   drain the queue continuously. Replaces the prior subprocess
                   fan-out via asyncio.Semaphore.
  1b legacy      — harness skills list (currently empty; slot reserved)
  2  allocator   — scripts/run_mike_allocator.py (programmatic)
  3  heartbeat   — scripts/run_scheduled_skill.sh hourly-review (still harness)

The queue uses a routine `coalesce_key` per (agent, symbol) so repeated hourly
primes don't stack unbounded if the workers fall behind — a new ticker_review
job is suppressed if one with the same key is still queued/running. OCAP-
triggered jobs (priority=5) preempt routine ticker_review (10) and
sector_summary (20) — workers pick highest-priority first.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import find_dotenv, load_dotenv
    found = find_dotenv(usecwd=True)
    if found:
        load_dotenv(found)
except Exception:
    pass


PYTHON = os.environ.get("PYTHON_BIN") or str(_REPO_ROOT / ".venv" / "bin" / "python")
SKILL_TIMEOUT_SEC = int(os.environ.get("SKILL_TIMEOUT_SEC", "900"))
ALLOCATOR_TIMEOUT_SEC = int(os.environ.get("ALLOCATOR_TIMEOUT_SEC", "180"))
HEARTBEAT_TIMEOUT_SEC = int(os.environ.get("HEARTBEAT_TIMEOUT_SEC", "900"))

AZ = ZoneInfo("America/Phoenix")

from meta_agent.queue_primer import (
    PIPELINE_SECTORS,
    prime_agent_queue as _prime_queue_for_agent,
    prime_all_agent_queues,
)

# Director skills (mike-morning/midday/allocator etc.) run on their own
# systemd timers; sector reviews/evenings/model-tunes run via the Python
# pipeline. Slot kept for future harness-routed additions.
HARNESS_SKILLS: list[str] = []


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _is_quiet_or_weekend(now_az: datetime | None = None) -> tuple[bool, bool]:
    now = now_az or datetime.now(AZ)
    is_quiet = now.hour >= 22 or now.hour < 5
    is_weekend = now.isoweekday() >= 6  # 6=Sat, 7=Sun
    return is_quiet, is_weekend


async def _run_subprocess(args: list[str], log_path: Path, timeout_sec: int) -> dict:
    """Run a subprocess with stdout+stderr appended to log_path.

    Returns: {args, exit_code, duration_ms, timed_out}.
    On timeout the child is SIGKILL'd; exit_code is -9 and timed_out=True.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    timed_out = False
    exit_code = -1

    with open(log_path, "ab") as logfd:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=logfd, stderr=logfd, cwd=str(_REPO_ROOT),
            )
        except FileNotFoundError as e:
            logfd.write(f"\n[orchestrator] FileNotFoundError: {e}\n".encode())
            return {"args": args, "exit_code": 127, "duration_ms": 0, "timed_out": False}

        try:
            exit_code = await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
            exit_code = -9

    return {
        "args": args,
        "exit_code": exit_code,
        "duration_ms": int((time.time() - started) * 1000),
        "timed_out": timed_out,
    }


# Indirection so unit tests can stub the whole subprocess layer.
_run_subprocess_impl: Callable[[list[str], Path, int], Awaitable[dict]] = _run_subprocess


async def _run_allocator() -> dict:
    return await _run_subprocess_impl(
        [PYTHON, str(_REPO_ROOT / "scripts" / "run_mike_allocator.py")],
        _REPO_ROOT / "logs" / "mike-allocator.log",
        ALLOCATOR_TIMEOUT_SEC,
    )


async def _run_heartbeat() -> dict:
    return await _run_subprocess_impl(
        [PYTHON, str(_REPO_ROOT / "scripts" / "run_hourly_review.py")],
        _REPO_ROOT / "logs" / "hourly-review.log",
        HEARTBEAT_TIMEOUT_SEC,
    )


async def _run_harness_skill(skill: str) -> dict:
    return await _run_subprocess_impl(
        ["bash", str(_REPO_ROOT / "scripts" / "run_scheduled_skill.sh"), skill],
        _REPO_ROOT / "logs" / f"{skill}.log",
        SKILL_TIMEOUT_SEC,
    )


def _phase_outcome_line(phase: str, skill: str, r: dict) -> str:
    dur = f"{r['duration_ms']}ms"
    if r["timed_out"]:
        return f"{phase}: {skill} TIMED OUT after {dur}"
    if r["exit_code"] == 0:
        return f"{phase}: {skill} ok ({dur})"
    if skill == "mike-allocator" and r["exit_code"] == 2:
        return f"{phase}: {skill} skipped by guard (exit=2, {dur})"
    return f"{phase}: {skill} exit={r['exit_code']} ({dur})"


async def main() -> int:
    start = time.time()
    _log(f"orchestrator start (pid={os.getpid()} python={PYTHON})")

    is_quiet, is_weekend = _is_quiet_or_weekend()
    if is_quiet or is_weekend:
        now_az = datetime.now(AZ)
        _log(f"skip fan-out (az_hour={now_az.hour} dow={now_az.isoweekday()} "
             f"quiet={is_quiet} weekend={is_weekend}); heartbeat only")
        hb = await _run_heartbeat()
        _log(_phase_outcome_line("phase 3", "hourly-review", hb))
        _log(f"orchestrator end (heartbeat-only path, {int((time.time()-start)*1000)}ms)")
        return 0

    # Phase 1a: prime the LLM-task queue. Workers consume continuously; the
    # orchestrator just lays down jobs (sector_summary + per-ticker reviews)
    # and returns. Coalesced re-runs are a no-op when prior hour's jobs
    # haven't drained yet.
    _log(f"phase 1a: priming queue for {len(PIPELINE_SECTORS)} sectors")
    result = await prime_all_agent_queues()
    for row in result["per_agent"]:
        if "error" in row:
            _log(f"phase 1a/{row['agent']}: ERROR {row['error']}")
        else:
            _log(f"phase 1a/{row['agent']}: enqueued={row['enqueued']} "
                 f"coalesced={row['coalesced']} (watchlist={row['watchlist_size']})")
    _log(f"phase 1a: primed {result['total_enqueued']} jobs, "
         f"{result['total_coalesced']} coalesced, "
         f"{len(result['failed_agents'])} agents failed ({result['duration_ms']}ms)")

    # Queue health heartbeat — gives the operator a one-line read on backlog.
    try:
        from db import store as _store
        stats = await _store.get_queue_stats()
        _log(f"phase 1a: queue stats {stats}")
    except Exception as e:
        _log(f"phase 1a: queue stats failed: {type(e).__name__}: {e}")

    # Phase 1b: legacy harness skills
    if HARNESS_SKILLS:
        _log(f"phase 1b: legacy harness skills: {HARNESS_SKILLS}")
        for skill in HARNESS_SKILLS:
            r = await _run_harness_skill(skill)
            _log(_phase_outcome_line(f"phase 1b/{skill}", skill, r))

    # Phase 2: mike-allocator (programmatic Python runner)
    _log("phase 2: running mike-allocator (programmatic)")
    alloc = await _run_allocator()
    _log(_phase_outcome_line("phase 2", "mike-allocator", alloc))

    # Phase 3: hourly-review heartbeat (still harness)
    _log("phase 3: running hourly-review heartbeat (harness)")
    hb = await _run_heartbeat()
    _log(_phase_outcome_line("phase 3", "hourly-review", hb))

    _log(f"orchestrator end (total={int((time.time()-start)*1000)}ms)")
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    except Exception as exc:
        import traceback
        _log(f"orchestrator crashed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        rc = 1
    sys.exit(rc)
