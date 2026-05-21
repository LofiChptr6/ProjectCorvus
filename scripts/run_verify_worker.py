"""Phase C of CITATION_ARCH (2026-05-21): conviction verification worker.

Polls `agent_conviction` for non-flat rows in the last N hours that don't yet
have a `conviction_verification` row, walks each conviction's citations,
checks them structurally (and via deterministic replay where possible), and
writes a verification row whose `action ∈ {pass, downgrade, reject}` the
allocator reads to gate sizing.

CoVe isolation principle (research §): the worker does NOT see the original
review prose or sibling citations. Each citation is checked against its
claimed evidence in isolation.

Current MVP scope:
  - Structural checks: evidence_id exists, kind matches snapshot, source_ref_id matches
  - Deterministic replay for `computed_indicator` citations: re-run the tool
    with the snapshot's inputs and confirm the same content_hash drops out
  - Existence checks for other kinds (news_post, model_run, prior_thesis,
    sibling_view)

Deferred to a follow-up:
  - LLM-based semantic check (CoVe stage 3 / Haiku-mediated "is the quote
    supported by the evidence?"). Stub returns 'unchecked' for now.

Run:
  $ python scripts/run_verify_worker.py [--since-hours 24] [--limit 200] [--once]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

log = logging.getLogger("verify_worker")


async def _verify_one_citation(citation: dict) -> dict:
    """Dispatch citation verification through the pipeline registry. Returns:
    {ok, reason: Optional[str], replay_ok: Optional[bool]}.

    Per-kind verification logic lives in meta_agent.citation_pipeline — adding
    a new kind only requires implementing CitationPipeline.verify() there."""
    from meta_agent.citation_pipeline import verify_citation
    res = await verify_citation(citation)
    return {"ok": res.ok, "reason": res.reason, "replay_ok": res.replay_ok}


def _aggregate_action(
    total: int, ok_count: int, flagged: list[dict]
) -> tuple[str, str]:
    """Decide the conviction-level action from per-citation results.
    Returns (action, notes)."""
    if total == 0:
        # No citations supplied. Phase A: allowed (citations are optional in
        # the soft-migration window). Phase D: not allowed for direction='long'.
        # Worker reports 'pass' but flags as 'no_citations'.
        return "pass", "no citations supplied (Phase A soft-migration)"
    if ok_count == total:
        return "pass", f"{ok_count}/{total} citations verified"
    if ok_count == 0:
        return "reject", f"all {total} citations failed verification"
    # Partial — downgrade
    return "downgrade", f"{ok_count}/{total} citations verified; rest flagged"


async def verify_conviction(conviction: dict) -> dict:
    """Walk all citations on a conviction, write a conviction_verification row,
    return a summary."""
    from db import store
    citations = conviction.get("citations") or []
    if not isinstance(citations, list):
        citations = []
    total = len(citations)
    ok_count = 0
    flagged: list[dict] = []
    for idx, cite in enumerate(citations):
        result = await _verify_one_citation(cite)
        if result["ok"]:
            ok_count += 1
        else:
            flagged.append({
                "idx": idx,
                "kind": cite.get("kind"),
                "evidence_id": cite.get("evidence_id"),
                "reason": result["reason"],
            })
    action, notes = _aggregate_action(total, ok_count, flagged)
    await store.write_conviction_verification(
        conviction_id=conviction["id"],
        citations_total=total,
        citations_ok=ok_count,
        citations_flagged=flagged or None,
        action=action,
        verifier_notes=notes,
    )
    return {
        "conviction_id": conviction["id"],
        "agent_name": conviction["agent_name"],
        "symbol": conviction["symbol"],
        "citations_total": total,
        "citations_ok": ok_count,
        "action": action,
        "notes": notes,
        "flagged": flagged,
    }


async def run_once(*, since_hours: float = 24.0, limit: int = 200) -> dict:
    """One pass: fetch unverified non-flat convictions, verify each, return a summary."""
    from db import store
    rows = await store.fetch_unverified_convictions(since_hours=since_hours, limit=limit)
    log.info("verify_worker: %d unverified convictions in last %sh", len(rows), since_hours)
    results: list[dict] = []
    for r in rows:
        try:
            res = await verify_conviction(r)
            results.append(res)
            log.info(
                "verified id=%s agent=%s sym=%s → action=%s (%d/%d ok)",
                res["conviction_id"], res["agent_name"], res["symbol"],
                res["action"], res["citations_ok"], res["citations_total"],
            )
        except Exception as exc:
            log.exception(
                "verify_worker failed on conviction_id=%s: %s", r["id"], exc,
            )
    return {
        "processed": len(results),
        "passed": sum(1 for r in results if r["action"] == "pass"),
        "downgraded": sum(1 for r in results if r["action"] == "downgrade"),
        "rejected": sum(1 for r in results if r["action"] == "reject"),
        "results": results,
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since-hours", type=float, default=24.0,
                        help="look back this many hours for unverified non-flat convictions")
    parser.add_argument("--limit", type=int, default=200,
                        help="max convictions to process per pass")
    parser.add_argument("--once", action="store_true",
                        help="single pass and exit (cron mode); default loops forever")
    parser.add_argument("--interval", type=float, default=300.0,
                        help="seconds between passes in loop mode")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.once:
        summary = await run_once(since_hours=args.since_hours, limit=args.limit)
        print(summary)
        return

    while True:
        try:
            await run_once(since_hours=args.since_hours, limit=args.limit)
        except Exception:
            log.exception("verify_worker pass crashed; sleeping then retrying")
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    asyncio.run(main())
