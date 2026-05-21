"""Phase C of CITATION_ARCH: conviction verification worker.

Covers:
  - upsert_conviction persists citations to the jsonb column
  - fetch_unverified_convictions returns only non-flat rows missing a verification row
  - write_conviction_verification + latest_verification round-trip
  - verify_conviction classifies action correctly (pass / downgrade / reject)
  - Replay check on computed_indicator citations
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _stamp_indicator_evidence(symbol: str, agent_name: str) -> dict:
    """Create a real evidence_snapshot row by running compute_indicator.
    Returns the full result dict including evidence_id."""
    from tools.analysis.compute_indicator import execute
    return await execute(symbol=symbol, indicator="RSI_14", agent_name=agent_name)


async def _seed_conviction_with_citations(
    agent_name: str, symbol: str, citations: list[dict] | None,
) -> int:
    from db import store
    return await store.upsert_conviction(
        agent_name=agent_name, symbol=symbol.upper(), direction="long",
        conviction=0.5, expected_return_pct=2.0, likelihood=0.6,
        time_to_target_days=5, expires_in_hours=24.0,
        rationale="citation test", citations=citations,
    )


# ── Persistence: citations round-trip the upsert ────────────────────────────


async def test_upsert_conviction_persists_citations(test_agent):
    from db import store
    from db.schema import get_pool
    cite = {"kind": "news_post", "evidence_id": 1,
            "source_ref_id": "post:test", "quote": "x"}
    conv_id = await _seed_conviction_with_citations(test_agent, "SPY", [cite])
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT citations FROM agent_conviction WHERE id = $1", conv_id,
        )
    assert row is not None
    import json
    stored = row["citations"]
    if isinstance(stored, str):
        stored = json.loads(stored)
    assert isinstance(stored, list)
    assert stored[0]["kind"] == "news_post"
    assert stored[0]["evidence_id"] == 1


async def test_upsert_conviction_accepts_none_citations(test_agent):
    """Phase A soft-migration: citations is optional. None must persist as NULL."""
    from db import store
    from db.schema import get_pool
    conv_id = await _seed_conviction_with_citations(test_agent, "SPY", None)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT citations FROM agent_conviction WHERE id = $1", conv_id,
        )
    assert row is not None
    assert row["citations"] is None


# ── fetch_unverified_convictions ────────────────────────────────────────────


async def test_fetch_unverified_skips_flat_rows(test_agent):
    """Flat rows aren't worth verifying — they don't drive sizing."""
    from db import store
    # Seed a flat row
    await store.upsert_conviction(
        agent_name=test_agent, symbol="FLAT_TEST", direction="flat",
        conviction=0.0, expected_return_pct=0.0, likelihood=0.0,
        time_to_target_days=0, expires_in_hours=4.0,
    )
    rows = await store.fetch_unverified_convictions(since_hours=1, limit=50)
    matching = [r for r in rows if r["agent_name"] == test_agent]
    assert all(r["direction"] != "flat" for r in matching)


async def test_fetch_unverified_excludes_already_verified(test_agent):
    """After a verification row is written, the conviction shouldn't reappear."""
    from db import store
    conv_id = await _seed_conviction_with_citations(test_agent, "SPY", None)
    # Initially appears in unverified list
    rows = await store.fetch_unverified_convictions(since_hours=1, limit=100)
    assert any(r["id"] == conv_id for r in rows)
    # Write a verification row
    await store.write_conviction_verification(
        conviction_id=conv_id, citations_total=0, citations_ok=0,
        citations_flagged=None, action="pass", verifier_notes="test",
    )
    # No longer appears in unverified list
    rows2 = await store.fetch_unverified_convictions(since_hours=1, limit=100)
    assert not any(r["id"] == conv_id for r in rows2)


# ── write_conviction_verification + latest_verification ─────────────────────


async def test_verification_round_trip(test_agent):
    from db import store
    conv_id = await _seed_conviction_with_citations(test_agent, "SPY", None)
    await store.write_conviction_verification(
        conviction_id=conv_id, citations_total=3, citations_ok=2,
        citations_flagged=[{"idx": 2, "reason": "test flag"}],
        action="downgrade", verifier_notes="2/3 ok",
    )
    v = await store.latest_verification(conv_id)
    assert v is not None
    assert v["action"] == "downgrade"
    assert v["citations_total"] == 3
    assert v["citations_ok"] == 2
    assert v["citations_flagged"][0]["reason"] == "test flag"


async def test_write_verification_rejects_bad_action(test_agent):
    from db import store
    conv_id = await _seed_conviction_with_citations(test_agent, "SPY", None)
    with pytest.raises(ValueError, match="action must be"):
        await store.write_conviction_verification(
            conviction_id=conv_id, citations_total=0, citations_ok=0,
            citations_flagged=None, action="probably_ok",
        )


async def test_latest_verification_picks_most_recent(test_agent):
    """When multiple verifications stack, latest_verification returns the newest."""
    from db import store
    conv_id = await _seed_conviction_with_citations(test_agent, "SPY", None)
    await store.write_conviction_verification(
        conviction_id=conv_id, citations_total=1, citations_ok=0,
        citations_flagged=None, action="reject", verifier_notes="first",
    )
    # Tiny sleep to ensure verified_at differs (NOW() resolution)
    import asyncio
    await asyncio.sleep(0.05)
    await store.write_conviction_verification(
        conviction_id=conv_id, citations_total=1, citations_ok=1,
        citations_flagged=None, action="pass", verifier_notes="second",
    )
    v = await store.latest_verification(conv_id)
    assert v["action"] == "pass"
    assert v["verifier_notes"] == "second"


# ── Worker classification ──────────────────────────────────────────────────


async def test_verify_conviction_pass_on_valid_citation(test_agent):
    """A conviction with one valid evidence-backed citation → action='pass'."""
    from scripts.run_verify_worker import verify_conviction
    from db import store
    ev = await _stamp_indicator_evidence("SBUX", test_agent)
    cite = {
        "kind": "computed_indicator",
        "evidence_id": ev["evidence_id"],
        "source_ref_id": f"SBUX:RSI_14:{ev['asof']}",
        "quote": f"SBUX RSI_14 = {ev['value']}",
    }
    conv_id = await _seed_conviction_with_citations(test_agent, "SBUX", [cite])
    conviction = {
        "id": conv_id, "agent_name": test_agent, "symbol": "SBUX",
        "direction": "long", "citations": [cite],
    }
    res = await verify_conviction(conviction)
    assert res["action"] == "pass"
    assert res["citations_ok"] == 1
    assert res["citations_total"] == 1


async def test_verify_conviction_reject_on_fabricated_citation(test_agent):
    """All citations fabricated (evidence_id doesn't exist) → action='reject'."""
    from scripts.run_verify_worker import verify_conviction
    cite = {
        "kind": "computed_indicator",
        "evidence_id": 999_999_999,
        "source_ref_id": "FAKE:fake:fake",
        "quote": "made-up indicator reading",
    }
    conv_id = await _seed_conviction_with_citations(test_agent, "SPY", [cite])
    conviction = {
        "id": conv_id, "agent_name": test_agent, "symbol": "SPY",
        "direction": "long", "citations": [cite],
    }
    res = await verify_conviction(conviction)
    assert res["action"] == "reject"
    assert res["citations_ok"] == 0


async def test_verify_conviction_downgrade_on_partial_pass(test_agent):
    """Mix of valid + fabricated → action='downgrade'."""
    from scripts.run_verify_worker import verify_conviction
    ev = await _stamp_indicator_evidence("SBUX", test_agent)
    valid = {
        "kind": "computed_indicator", "evidence_id": ev["evidence_id"],
        "source_ref_id": f"SBUX:RSI_14:{ev['asof']}",
        "quote": "real",
    }
    bad = {
        "kind": "computed_indicator", "evidence_id": 999_999_999,
        "source_ref_id": "FAKE", "quote": "fake",
    }
    conv_id = await _seed_conviction_with_citations(test_agent, "SBUX", [valid, bad])
    conviction = {
        "id": conv_id, "agent_name": test_agent, "symbol": "SBUX",
        "direction": "long", "citations": [valid, bad],
    }
    res = await verify_conviction(conviction)
    assert res["action"] == "downgrade"
    assert res["citations_ok"] == 1
    assert res["citations_total"] == 2
    assert len(res["flagged"]) == 1


async def test_verify_conviction_pass_on_no_citations(test_agent):
    """Phase A soft-migration: no citations is allowed; worker passes with note."""
    from scripts.run_verify_worker import verify_conviction
    conv_id = await _seed_conviction_with_citations(test_agent, "SPY", None)
    conviction = {
        "id": conv_id, "agent_name": test_agent, "symbol": "SPY",
        "direction": "long", "citations": None,
    }
    res = await verify_conviction(conviction)
    assert res["action"] == "pass"
    assert "no citations" in res["notes"]


async def test_verify_conviction_flags_kind_mismatch(test_agent):
    """Citation claims kind='news_post' but evidence row is kind='computed_indicator' → flag."""
    from scripts.run_verify_worker import verify_conviction
    ev = await _stamp_indicator_evidence("SBUX", test_agent)
    cite = {
        "kind": "news_post",       # WRONG — evidence is a computed_indicator
        "evidence_id": ev["evidence_id"],
        "source_ref_id": f"SBUX:RSI_14:{ev['asof']}",
        "quote": "lying about the kind",
    }
    conv_id = await _seed_conviction_with_citations(test_agent, "SBUX", [cite])
    conviction = {
        "id": conv_id, "agent_name": test_agent, "symbol": "SBUX",
        "direction": "long", "citations": [cite],
    }
    res = await verify_conviction(conviction)
    assert res["action"] == "reject"
    assert "kind mismatch" in res["flagged"][0]["reason"]
