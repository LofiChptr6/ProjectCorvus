"""Prime the LLM-task queue for sector agents.

Phase 1a of the hourly orchestrator extracted into a reusable module so both
the cron orchestrator (`scripts/run_hourly_orchestrator.py`) and on-demand
callers (the `prime_sector_queues` MCP tool, future CLI) share one code
path. Idempotent: `coalesce_key` dedupes re-priming within the 1-hour window,
so calling repeatedly is safe — repeat calls become no-ops on jobs already
queued/running.

The queue workers (`trading-queue-worker@*`) drain the table continuously;
they still gate execution on kill_switch and the AZ quiet window. Priming
the queue does not bypass any safety control.
"""
from __future__ import annotations

import asyncio
import time

PIPELINE_SECTORS = ["atlas", "commodity", "energy", "fab", "fabless", "iron",
                    "maya", "rex", "trump", "vera", "volt"]


async def prime_agent_queue(agent: str) -> dict:
    """Insert one sector_summary + N ticker_review jobs (N = active watchlist
    size) for one agent. Returns per-agent counts."""
    from db import store

    res = await store.enqueue_job_coalesced(
        agent_name=agent,
        job_type="sector_summary",
        payload={"trigger": "hourly_orchestrator"},
        priority=20,
        coalesce_key=f"routine:{agent}:sector_summary",
        coalesce_window_s=3600,
        triggers_seen=["hourly"],
    )
    summary_enqueued = 1 if res["action"] == "enqueued" else 0
    summary_coalesced = 1 if res["action"] == "coalesced" else 0

    wl_rows = await store.load_agent_watchlist(agent)
    tr_enqueued = 0
    tr_coalesced = 0
    for r in wl_rows:
        sym = r["symbol"]
        out = await store.enqueue_job_coalesced(
            agent_name=agent,
            job_type="ticker_review",
            payload={"symbol": sym, "trigger": "hourly_orchestrator"},
            priority=10,
            coalesce_key=f"routine:{agent}:{sym}",
            coalesce_window_s=3600,
            triggers_seen=["hourly"],
        )
        if out["action"] == "enqueued":
            tr_enqueued += 1
        else:
            tr_coalesced += 1

    return {
        "agent": agent,
        "sector_summary_enqueued": summary_enqueued,
        "sector_summary_coalesced": summary_coalesced,
        "ticker_review_enqueued": tr_enqueued,
        "ticker_review_coalesced": tr_coalesced,
        "watchlist_size": len(wl_rows),
    }


async def prime_all_agent_queues() -> dict:
    """Fan-out prime over every sector in `PIPELINE_SECTORS`.

    Returns:
        {
          "total_enqueued": int,
          "total_coalesced": int,
          "failed_agents": [agent, ...],
          "duration_ms": int,
          "per_agent": [
            {"agent": str, "enqueued": int, "coalesced": int,
             "watchlist_size": int}  OR  {"agent": str, "error": str},
            ...
          ],
        }
    """
    started = time.time()
    results = await asyncio.gather(
        *(prime_agent_queue(s) for s in PIPELINE_SECTORS),
        return_exceptions=True,
    )
    per_agent: list[dict] = []
    total_enq = 0
    total_coalesced = 0
    failed: list[str] = []
    for agent, r in zip(PIPELINE_SECTORS, results):
        if isinstance(r, Exception):
            failed.append(agent)
            per_agent.append({"agent": agent, "error": f"{type(r).__name__}: {r}"})
            continue
        enq = r["sector_summary_enqueued"] + r["ticker_review_enqueued"]
        coal = r["sector_summary_coalesced"] + r["ticker_review_coalesced"]
        total_enq += enq
        total_coalesced += coal
        per_agent.append({
            "agent": agent,
            "enqueued": enq,
            "coalesced": coal,
            "watchlist_size": r["watchlist_size"],
        })
    return {
        "total_enqueued": total_enq,
        "total_coalesced": total_coalesced,
        "failed_agents": failed,
        "duration_ms": int((time.time() - started) * 1000),
        "per_agent": per_agent,
    }
