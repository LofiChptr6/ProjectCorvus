#!/usr/bin/env python3
"""Durable LLM-task queue worker.

Long-running daemon. Loops:
  1. Poll agent_job (SELECT FOR UPDATE SKIP LOCKED, priority + enqueued_at).
  2. Gate at pick-time: quiet window only (DESK_POLICY §6, 22:00–05:00 MST).
     Quieted jobs flip to status='skipped' with the reason in `error`.
     Kill switch is NOT enforced here — it gates the allocator
     (`run_mike_allocator._guard_skip`) and the order layer
     (`risk/checks/kill_switch.py`, `place_order.py`). Agents are free to
     analyze and publish convictions while killed; mike just won't act on
     them. Per-agent kill is enforced at the allocator's conviction-load
     step (`db.store.get_active_convictions`).
  3. Dispatch by job_type → subprocess(scripts/run_skill.py <agent> review).
     Each job runs serially inside one worker; horizontal scale is by
     running multiple workers (systemctl --user start trading-queue-worker@N).
  4. Flip status to done/failed and loop.

Why subprocess: workers are intentionally cheap. They don't load agent
bundlers or hit vLLM directly — `run_skill.py` already does that
end-to-end. The worker's job is just to feed jobs into that pipeline,
provide cross-worker concurrency via FOR UPDATE SKIP LOCKED, and write
back status. Cross-process vLLM throttling (multiple workers + harness
+ concierge all hitting one vLLM) is a v2 — currently each worker is
serial so total inflight = worker_count + ambient concierge / Claude
Code sessions.

OCAP-triggered jobs (priority=5) get inbox context written before the
subprocess runs so the review knows what woke it.

Manual:
    python scripts/run_queue_worker.py              # long-running
    python scripts/run_queue_worker.py --once       # one job, then exit
    python scripts/run_queue_worker.py --max-jobs N # drain N jobs, then exit
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
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

log = logging.getLogger("queue_worker")

AZ = ZoneInfo("America/Phoenix")
POLL_INTERVAL_S = 2.0
SUBPROCESS_TIMEOUT_S = int(os.environ.get("QUEUE_WORKER_JOB_TIMEOUT_S", "900"))
PYTHON = os.environ.get("PYTHON_BIN") or str(_REPO_ROOT / ".venv" / "bin" / "python")
HEARTBEAT_INTERVAL_S = 300


def _is_quiet_window(now_az: datetime | None = None) -> bool:
    """Mirrors run_hourly_orchestrator._is_quiet_or_weekend, modulo evening
    pipelines (which are allowed). Returns True for 22:00–05:00 MST
    + weekends; the dispatcher decides per-job-type whether to enforce."""
    now = now_az or datetime.now(AZ)
    return (now.hour >= 22 or now.hour < 5) or now.isoweekday() >= 6


# Job types that are gated by the quiet window. Evening reviews run during
# quiet window by design (see DESK_POLICY §6); future evening jobs would
# be added here too.
_QUIET_GATED_JOB_TYPES = {
    "ticker_review", "sector_summary", "ocap_triggered_review",
}


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def _write_ocap_inbox_context(agent_name: str, payload: dict, triggers_seen: Optional[list]) -> None:
    """Drop a short inbox note so the review session sees what woke it."""
    from db import store
    triggers = triggers_seen or payload.get("triggers") or []
    body = (
        f"OCAP wake-up: {payload.get('symbol','?')} tripped "
        f"{', '.join(triggers) or 'rule(s)'} on bar {payload.get('bar_time','?')}."
    )
    try:
        await store.post_to_inbox(agent_name=agent_name, sender="ocap", body=body)
    except Exception as e:
        log.warning("post_to_inbox(%s): %s: %s", agent_name, type(e).__name__, e)


async def _dispatch_quant_distribution(job: dict, payload: dict) -> tuple[int, dict | None]:
    """In-process handler for `quant_distribution_compute` jobs (Phase F).

    Payload shape: {model_name: str, symbol: str}. The handler:
      1. Loads agents/<agent>/models/<model>.py via the existing runner.
      2. Runs compute(); on success, distributions are auto-persisted to
         agent_forecast under a fresh forecast_run_id by the runner.
      3. NEVER writes to agent_conviction — this is a research path, not a
         trading path. The mixer / live allocator picks up distributions
         from agent_forecast.

    Returns (exit_code, skill_result). Exit code 0 on success (including
    deliberate skips), 1 on validation/load error. The skill_result rollup
    captures status / forecast_run_id / number of horizons for debugging.
    """
    from meta_agent.conviction_from_model import compute_conviction_payload

    agent = job["agent_name"]
    model_name = payload.get("model_name") or ""
    symbol = payload.get("symbol") or ""
    if not model_name or not symbol:
        return 1, {"status": "error", "error": "payload missing model_name or symbol"}

    try:
        res = await compute_conviction_payload(agent, model_name, symbol)
    except Exception as exc:
        return 1, {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    status = res.get("status")
    rollup: dict = {
        "status": status,
        "agent": agent,
        "model": model_name,
        "symbol": symbol,
        "model_version": res.get("model_version"),
    }
    if status == "ok":
        p = res.get("payload") or {}
        rollup.update({
            "direction": p.get("direction"),
            "conviction": p.get("conviction"),
            "forecast_run_id": p.get("forecast_run_id"),
            "functional_name": p.get("functional_name"),
        })
    elif status == "skipped":
        rollup["reason"] = res.get("reason")
    elif status == "error":
        rollup["error"] = res.get("error")

    # ok / skipped are both successful queue outcomes (the model ran cleanly).
    # error means the model crashed or rejected its inputs — exit 1 so the
    # worker marks the job failed and operators investigate.
    exit_code = 0 if status in {"ok", "skipped"} else 1
    return exit_code, rollup


async def _dispatch_ocap_rebalance(job: dict) -> tuple[int, dict | None]:
    """Subprocess handler for `ocap_rebalance` jobs (Phase 1b).

    Fires the same `scripts/run_mike_allocator.py` that the hourly orchestrator's
    phase 2 uses — single canonical allocator entry, no duplicated logic.
    The allocator's own `_guard_skip` enforces kill_switch / market_hours /
    quiet_window, and its Postgres advisory lock serializes concurrent runs
    (hourly cron + queue-fired). This dispatcher just forwards.

    Coalescing at enqueue time (window=60s, key='ocap:rebalance') means a
    flurry of OCAP-completed reviews collapses to at most one rebalance per
    minute, well under the rebalance_desk 12/60s rate limit.

    Logging: subprocess stdout+stderr is captured to a buffer and written to
    BOTH `logs/mike-allocator.log` (the allocator's canonical log) AND
    `logs/queue-worker-ocap_rebalance.log` (a per-job-type tail). Lets you
    debug one OCAP-fired rebalance without grep-joining two files.
    """
    args = [PYTHON, str(_REPO_ROOT / "scripts" / "run_mike_allocator.py")]
    log_dir = Path(os.environ.get("LOG_DIR") or (_REPO_ROOT / "logs"))
    allocator_log = log_dir / "mike-allocator.log"
    worker_log = log_dir / "queue-worker-ocap_rebalance.log"
    allocator_log.parent.mkdir(parents=True, exist_ok=True)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        cwd=str(_REPO_ROOT),
    )
    try:
        stdout_b, _ = await asyncio.wait_for(
            proc.communicate(), timeout=SUBPROCESS_TIMEOUT_S,
        )
        exit_code = proc.returncode if proc.returncode is not None else -1
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass
        return -9, {"status": "error", "error": "allocator subprocess timeout"}

    header = f"\n--- ocap_rebalance job_id={job['id']} ---\n".encode()
    footer = f"--- ocap_rebalance job_id={job['id']} exit={exit_code} ---\n".encode()
    for path in (allocator_log, worker_log):
        try:
            with open(path, "ab") as f:
                f.write(header)
                if stdout_b:
                    f.write(stdout_b)
                f.write(footer)
        except OSError as exc:
            log.warning("tee allocator output to %s failed: %s", path, exc)

    # exit=2 means the allocator's own guard skipped (kill/quiet/market) or
    # the advisory lock was held — not a failure, just "nothing to do right
    # now". Map both 0 and 2 to a 0 return so the worker's `mark_job_done`
    # path fires; the skill_result rollup carries the actual semantic.
    rollup_status = "ok" if exit_code == 0 else ("skipped" if exit_code == 2 else "error")
    worker_exit = 0 if exit_code in (0, 2) else 1
    return worker_exit, {"status": rollup_status, "exit_code": exit_code}


async def _dispatch(job: dict) -> tuple[int, dict | None]:
    """Run one job to completion. Returns (exit_code, skill_result_dict).

    Routing:
      - `quant_distribution_compute` (Phase F: HMM, LightGBM, any heavy
        distribution-emitting model) runs in-process via
        _dispatch_quant_distribution. Fast path, no subprocess.
      - All other job_types map to scripts/run_skill.py subprocess.

    The skill_result is parsed from run_skill.py's stdout JSON line for
    subprocess jobs, or returned directly by the in-process handler. It's
    persisted on the agent_job row so operators can debug queue-driven
    work without grepping logs."""
    job_type = job["job_type"]
    agent = job["agent_name"]

    payload = job["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    triggers_seen = job.get("triggers_seen")
    if isinstance(triggers_seen, str):
        triggers_seen = json.loads(triggers_seen)

    if job_type == "quant_distribution_compute":
        # Bound the in-process model fit so a hung HMM/LightGBM call doesn't
        # park the worker. SUBPROCESS_TIMEOUT_S is generous (15min default)
        # but distribution computes should land in seconds.
        try:
            return await asyncio.wait_for(
                _dispatch_quant_distribution(job, payload or {}),
                timeout=SUBPROCESS_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return -9, {"status": "error", "error": "in-process timeout"}

    if job_type == "ocap_rebalance":
        return await _dispatch_ocap_rebalance(job)

    if job_type == "ocap_triggered_review":
        await _write_ocap_inbox_context(agent, payload or {}, triggers_seen)

    args = [PYTHON, str(_REPO_ROOT / "scripts" / "run_skill.py"), agent, "review"]
    log_dir = Path(os.environ.get("LOG_DIR") or (_REPO_ROOT / "logs"))
    log_path = log_dir / f"queue-worker-{agent}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Pass job_id via env so run_skill.py → runner → bundler can render
    # "what woke me up". Capture stdout (the run_skill.py rollup JSON line)
    # so we can persist it on agent_job.skill_result.
    env = {**os.environ, "JOB_ID": str(job["id"])}

    # stderr → log file; stdout → captured for the JSON rollup line.
    with open(log_path, "ab") as logfd:
        logfd.write(
            f"\n--- job_id={job['id']} type={job_type} priority={job['priority']} "
            f"payload={json.dumps(payload, default=str)} ---\n".encode()
        )
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE, stderr=logfd,
            cwd=str(_REPO_ROOT), env=env,
        )
        try:
            stdout_b, _ = await asyncio.wait_for(
                proc.communicate(), timeout=SUBPROCESS_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
            return -9, None

        # Also tee the captured stdout into the log so the per-agent log is
        # the single source of truth for "what did this subprocess do."
        if stdout_b:
            logfd.write(b"--- run_skill.py stdout ---\n")
            logfd.write(stdout_b)
            logfd.write(b"--- end stdout ---\n")

    # Parse the last non-empty JSON line from stdout (run_skill.py emits
    # one rollup line at the end; other log lines route through stderr).
    skill_result: dict | None = None
    if stdout_b:
        for line in reversed(stdout_b.decode("utf-8", errors="replace").splitlines()):
            line = line.strip()
            if not line:
                continue
            if line.startswith("{") and line.endswith("}"):
                try:
                    skill_result = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

    return proc.returncode if proc.returncode is not None else -1, skill_result


async def _one_iteration(worker_id: str) -> str:
    """Pull one job, dispatch it, update status. Returns a short status code
    used by the heartbeat ('idle' | 'done' | 'failed' | 'skipped' | 'error')."""
    from db import store

    job = await store.pick_next_job(worker_id)
    if not job:
        return "idle"

    if _is_quiet_window() and job["job_type"] in _QUIET_GATED_JOB_TYPES:
        await store.mark_job_skipped(job["id"], "quiet window (DESK_POLICY §6)")
        log.info("skip job=%d %s/%s: quiet window", job["id"], job["agent_name"], job["job_type"])
        return "skipped"

    log.info("run job=%d %s/%s priority=%d", job["id"], job["agent_name"], job["job_type"], job["priority"])
    started = time.monotonic()
    try:
        exit_code, skill_result = await _dispatch(job)
    except Exception as exc:
        await store.mark_job_failed(job["id"], f"{type(exc).__name__}: {exc}")
        log.exception("job=%d crashed: %s", job["id"], exc)
        return "error"

    # Persist the parsed rollup (session_id, iterations, finish, validation,
    # write_summary, etc.) so debugging a queue-driven skill doesn't require
    # log-grepping.
    if skill_result is not None:
        try:
            await store.set_job_skill_result(job["id"], skill_result)
        except Exception as exc:
            log.warning("set_job_skill_result(%d) failed: %s: %s",
                        job["id"], type(exc).__name__, exc)

    dur_ms = int((time.monotonic() - started) * 1000)
    if exit_code == 0:
        await store.mark_job_done(job["id"])
        sid = (skill_result or {}).get("session_id")
        log.info("done job=%d session=%s (%dms)", job["id"], (sid or "")[:8], dur_ms)
        # An OCAP-fired review just completed. Fire a debounced mike-allocator
        # run ONLY if the conviction stack materially changed — re-publishing
        # an identical view should not trigger a no-op rebalance + Telegram
        # spam. The runner attaches `convictions_materially_changed` to the
        # rollup; we read it here. Coalesce window=60s collapses bursts; the
        # allocator's advisory lock serializes runs.
        if job["job_type"] == "ocap_triggered_review":
            write_summary = (skill_result or {}).get("write_summary") or {}
            n_material = int(write_summary.get("convictions_materially_changed", 0))
            if n_material == 0:
                log.info("ocap review job=%d wrote no material conviction changes; "
                         "skipping ocap_rebalance enqueue", job["id"])
            else:
                payload = job.get("payload")
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = None
                try:
                    await store.enqueue_job_coalesced(
                        agent_name="mike",
                        job_type="ocap_rebalance",
                        payload={
                            "trigger": "ocap_review_completed",
                            "source_job_id": job["id"],
                            "source_agent": job["agent_name"],
                            "source_symbol": (payload or {}).get("symbol"),
                            "convictions_materially_changed": n_material,
                        },
                        priority=5,
                        coalesce_key="ocap:rebalance",
                        coalesce_window_s=60,
                        triggers_seen=["ocap"],
                    )
                except Exception as exc:
                    log.warning("enqueue ocap_rebalance after job=%d failed: %s: %s",
                                job["id"], type(exc).__name__, exc)
        return "done"
    msg = f"subprocess exit={exit_code}" + (" (timed out)" if exit_code == -9 else "")
    await store.mark_job_failed(job["id"], msg)
    log.info("fail job=%d %s (%dms)", job["id"], msg, dur_ms)
    return "failed"


async def _heartbeat() -> None:
    from db import store
    try:
        stats = await store.get_queue_stats()
        log.info("queue heartbeat: %s", stats)
    except Exception as e:
        log.warning("heartbeat failed: %s: %s", type(e).__name__, e)


async def main(once: bool, max_jobs: Optional[int]) -> int:
    worker_id = _worker_id()
    log.info("worker up id=%s once=%s max_jobs=%s", worker_id, once, max_jobs)

    processed = 0
    last_heartbeat = time.monotonic()
    while True:
        status = await _one_iteration(worker_id)
        if status != "idle":
            processed += 1
            if max_jobs and processed >= max_jobs:
                log.info("worker exit: hit max_jobs=%d", max_jobs)
                return 0
        if once:
            log.info("worker exit: --once after %s", status)
            return 0
        if status == "idle":
            if time.monotonic() - last_heartbeat > HEARTBEAT_INTERVAL_S:
                await _heartbeat()
                last_heartbeat = time.monotonic()
            await asyncio.sleep(POLL_INTERVAL_S)


def cli() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--once", action="store_true", help="Process one job (or idle once) then exit")
    p.add_argument("--max-jobs", type=int, default=None, help="Exit after N successful jobs")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return asyncio.run(main(once=args.once, max_jobs=args.max_jobs))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(cli())
