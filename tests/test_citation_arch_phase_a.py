"""Phase A of CITATION_ARCH: evidence_snapshot helpers, Citation schema,
and the three new harness tools (compute_indicator, query_news, verify_catalyst).

Covers the contract surfaces — table semantics, dedupe behavior, schema
validation, and tool failure modes. Live-data integration is covered by
the smoke tests in the dev workflow, not here.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ── evidence_snapshot helpers ────────────────────────────────────────────────


async def test_stamp_evidence_dedupes_identical_content(test_agent):
    """Same kind+ref+outputs → same evidence_id (UNIQUE constraint)."""
    from db import store
    id1 = await store.stamp_evidence(
        kind="computed_indicator",
        source_ref_id=f"TEST:RSI_14:{test_agent}",
        outputs_json={"value": 50.0, "n_bars": 100},
        inputs_json={"symbol": "TEST", "indicator": "RSI_14"},
        content_snippet="TEST RSI_14 = 50.0",
        computed_by="test@0",
        agent_name=test_agent,
    )
    id2 = await store.stamp_evidence(
        kind="computed_indicator",
        source_ref_id=f"TEST:RSI_14:{test_agent}",
        outputs_json={"value": 50.0, "n_bars": 100},
        inputs_json={"symbol": "TEST", "indicator": "RSI_14"},
        content_snippet="TEST RSI_14 = 50.0",
        computed_by="test@0",
        agent_name=test_agent,
    )
    assert id1 == id2, "identical outputs should dedupe to the same evidence_id"


async def test_stamp_evidence_distinct_outputs_get_distinct_rows(test_agent):
    """Different outputs under the same ref produce distinct rows.
    Preserves the audit trail across re-computations as the data evolves."""
    from db import store
    id1 = await store.stamp_evidence(
        kind="computed_indicator",
        source_ref_id=f"TEST:RSI_14:{test_agent}-2",
        outputs_json={"value": 50.0},
        computed_by="test@0",
        agent_name=test_agent,
    )
    id2 = await store.stamp_evidence(
        kind="computed_indicator",
        source_ref_id=f"TEST:RSI_14:{test_agent}-2",
        outputs_json={"value": 60.0},
        computed_by="test@0",
        agent_name=test_agent,
    )
    assert id1 != id2


async def test_stamp_evidence_rejects_unknown_kind(test_agent):
    from db import store
    with pytest.raises(ValueError, match="kind must be one of"):
        await store.stamp_evidence(
            kind="invented_kind",
            source_ref_id="x",
            outputs_json={},
            computed_by="test@0",
            agent_name=test_agent,
        )


async def test_stamp_evidence_requires_source_ref_id_and_computed_by(test_agent):
    from db import store
    with pytest.raises(ValueError, match="source_ref_id"):
        await store.stamp_evidence(
            kind="news_post",
            source_ref_id="",
            outputs_json={},
            computed_by="test@0",
        )
    with pytest.raises(ValueError, match="computed_by"):
        await store.stamp_evidence(
            kind="news_post",
            source_ref_id="x",
            outputs_json={},
            computed_by="",
        )


async def test_get_evidence_snapshot_returns_parsed_json(test_agent):
    from db import store
    eid = await store.stamp_evidence(
        kind="computed_indicator",
        source_ref_id=f"TEST:roundtrip:{test_agent}",
        inputs_json={"a": 1},
        outputs_json={"b": 2},
        computed_by="test@0",
        agent_name=test_agent,
    )
    row = await store.get_evidence_snapshot(eid)
    assert row is not None
    assert row["kind"] == "computed_indicator"
    assert row["inputs_json"] == {"a": 1}, "inputs_json must round-trip as dict"
    assert row["outputs_json"] == {"b": 2}


async def test_get_evidence_snapshot_missing_returns_none():
    from db import store
    # Use a deliberately-large id unlikely to exist.
    assert await store.get_evidence_snapshot(2**62) is None


# ── Citation schema ─────────────────────────────────────────────────────────


def test_citation_accepts_valid_kinds():
    from pipelines.schemas import Citation
    for kind in ("news_post", "model_run", "computed_indicator",
                 "prior_thesis", "sibling_view"):
        c = Citation(kind=kind, evidence_id=1, source_ref_id="x", quote="y")
        assert c.kind == kind


def test_citation_rejects_invalid_kind():
    from pipelines.schemas import Citation
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        Citation(kind="wikipedia", evidence_id=1, source_ref_id="x", quote="y")


def test_citation_rejects_zero_or_negative_evidence_id():
    from pipelines.schemas import Citation
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        Citation(kind="news_post", evidence_id=0, source_ref_id="x", quote="y")


def test_citation_truncates_overly_long_quote():
    """quote is hard-capped at 300 chars; longer values are rejected so the
    LLM cannot dump a full article into the citation field."""
    from pipelines.schemas import Citation
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        Citation(
            kind="news_post", evidence_id=1, source_ref_id="x",
            quote="x" * 301,
        )


def test_conviction_view_accepts_citations_field():
    """Phase A: citations is optional on ConvictionView."""
    from pipelines.schemas import Citation, ConvictionView
    cite = Citation(kind="news_post", evidence_id=1, source_ref_id="post:1", quote="q")
    v = ConvictionView(
        symbol="SPY", direction="long",
        expected_return_pct=2.0, likelihood=0.6, time_to_target_days=5,
        expires_in_hours=24.0, citations=[cite],
    )
    assert v.citations is not None
    assert v.citations[0].kind == "news_post"


def test_conviction_view_omits_citations_field():
    """Phase A: citations field is optional and defaults to None."""
    from pipelines.schemas import ConvictionView
    v = ConvictionView(symbol="SPY", direction="flat", expires_in_hours=4.0)
    assert v.citations is None


# ── compute_indicator (light unit checks — full smoke covered by dev workflow) ─


async def test_compute_indicator_rejects_unknown_indicator(test_agent):
    from tools.analysis.compute_indicator import execute
    out = await execute(symbol="SPY", indicator="STOCHASTIC_OSCILLATOR", agent_name=test_agent)
    assert out["ok"] is False
    assert "indicator must be one of" in out["reason"]


async def test_compute_indicator_handles_missing_symbol(test_agent):
    from tools.analysis.compute_indicator import execute
    out = await execute(symbol="ZZZ_NOT_REAL", indicator="RSI_14", agent_name=test_agent)
    assert out["ok"] is False
    assert "no local_bars_daily" in out["reason"]


async def test_compute_indicator_rejects_malformed_asof(test_agent):
    from tools.analysis.compute_indicator import execute
    out = await execute(symbol="SPY", indicator="RSI_14", asof="not-a-date", agent_name=test_agent)
    assert out["ok"] is False
    assert "asof" in out["reason"]


# ── query_news ───────────────────────────────────────────────────────────────


async def test_query_news_requires_nonempty_terms(test_agent):
    from tools.analysis.query_news import execute
    out = await execute(terms=[], agent_name=test_agent)
    assert out["ok"] is False
    assert "terms" in out["reason"]


async def test_query_news_returns_evidence_id_for_empty_match(test_agent):
    """Absence of evidence is itself evidence — empty match still stamps a row."""
    from tools.analysis.query_news import execute
    out = await execute(
        terms=[f"completely_invented_term_{test_agent}"],
        window_days=1,
        agent_name=test_agent,
    )
    assert out["ok"] is True
    assert out["match_count"] == 0
    assert out["evidence_id"] > 0


# ── verify_catalyst ──────────────────────────────────────────────────────────


async def test_verify_catalyst_rejects_malformed_date(test_agent):
    from tools.analysis.verify_catalyst import execute
    out = await execute("anything", date="not-a-date", agent_name=test_agent)
    assert out["ok"] is False
    assert "date" in out["reason"]


async def test_verify_catalyst_returns_absent_for_fictional_event(test_agent):
    from tools.analysis.verify_catalyst import execute
    out = await execute(
        f"completely_invented_catalyst_{test_agent}",
        date="2026-06-15",
        agent_name=test_agent,
    )
    assert out["ok"] is True
    assert out["found"] is False
    assert out["confidence"] == "absent"
    assert out["evidence_id"] > 0


async def test_verify_catalyst_requires_event_text(test_agent):
    from tools.analysis.verify_catalyst import execute
    out = await execute("", date="2026-06-15", agent_name=test_agent)
    assert out["ok"] is False
