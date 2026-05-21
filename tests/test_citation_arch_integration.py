"""Integration tests for CITATION_ARCH end-to-end:

  (a) runner.py threading — full review pipeline writes citations to agent_conviction
  (b) from_model auto-citation — model_run citation minted + verified
  (c) MCP tool surface — the @mcp.tool() wrappers are callable + return expected JSON
  (f) skill → citation — run_skill returns evidence_id; that id round-trips to a
      Citation on a ConvictionView and verifies cleanly

These exercise actual code paths (not just unit-level helpers). Most rely on
real local_bars_daily + post (news-headlines) data being present; failures here
suggest either bar-streamer dead or news ingest dead.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest
import pytest_asyncio

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipelines import llm_client, runner


# ── Fake LLM client (lifted from test_review_pipeline.py) ────────────────────


@dataclass
class _FakeFunction:
    name: str
    arguments: str


@dataclass
class _FakeToolCall:
    id: str
    function: _FakeFunction
    type: str = "function"


@dataclass
class _FakeMessage:
    content: Optional[str]
    tool_calls: Optional[list[_FakeToolCall]] = None


@dataclass
class _FakeChoice:
    finish_reason: str
    message: _FakeMessage


@dataclass
class _FakeUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class _FakeResponse:
    choices: list[_FakeChoice]
    usage: Optional[_FakeUsage] = None


class _FakeCompletions:
    def __init__(self, scripted):
        self._scripted = list(scripted)

    async def create(self, **kwargs):
        if not self._scripted:
            raise RuntimeError("scripted responses exhausted")
        return self._scripted.pop(0)


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeOpenAI:
    def __init__(self, scripted):
        self.chat = _FakeChat(_FakeCompletions(scripted))


def _stub_llm_client(scripted):
    return llm_client.LLMClient(
        client=_FakeOpenAI(scripted),
        base_url="http://fake/v1",
        session_id="test-citation-sess",
        model="fake-model",
    )


def _final_text_response(text: str) -> _FakeResponse:
    return _FakeResponse(choices=[_FakeChoice(
        finish_reason="stop",
        message=_FakeMessage(content=text),
    )])


async def _seed_evidence(symbol: str, agent_name: str) -> dict:
    """Stamp a real evidence row via compute_indicator and return its result.
    Used by tests that need a citation to point at."""
    from tools.analysis.compute_indicator import execute
    return await execute(symbol=symbol, indicator="RSI_14", agent_name=agent_name)


# ── (a) Runner threading: ConvictionView.citations → agent_conviction.citations ──


async def test_a_runner_persists_llm_authored_citations(test_agent, monkeypatch):
    """LLM emits a ConvictionView with citations[]. Runner threads them into
    upsert_conviction. Verify they round-trip to the citations jsonb column,
    then verify_worker grades the conviction."""
    from db import store
    from db.schema import get_pool

    # 1. Stamp a real evidence row first
    ev = await _seed_evidence("SBUX", test_agent)
    assert ev["ok"], f"compute_indicator declined: {ev.get('reason')}"

    # 2. Build a ReviewOutput with that citation attached to a long view
    review_json = json.dumps({
        "views": [{
            "symbol": "SBUX", "direction": "long",
            "expected_return_pct": 1.2, "likelihood": 0.6,
            "time_to_target_days": 5, "expires_in_hours": 24,
            "rationale": "Trend constructive (RSI evidence cited).",
            "citations": [{
                "kind": "computed_indicator",
                "evidence_id": ev["evidence_id"],
                "source_ref_id": f"SBUX:RSI_14:{ev['asof']}",
                "quote": f"SBUX RSI_14 = {ev['value']}",
            }],
        }],
        "forecasts": [],
        "theses_to_record": [],
        "theses_to_grade": [],
        "telegram_summary": f"{test_agent}: 1 long with citation",
    })
    scripted = [_final_text_response(review_json)]
    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_llm_client(scripted),
    )

    # 3. Run the full review pipeline
    result = await runner.run_skill(test_agent, "review", dry_run=False)
    assert result.write_summary is not None
    assert result.write_summary["views_inserted"] == 1

    # 4. Confirm citations landed on agent_conviction
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, citations FROM agent_conviction "
            "WHERE agent_name=$1 AND symbol='SBUX'",
            test_agent,
        )
    assert row is not None
    cites = row["citations"]
    if isinstance(cites, str):
        cites = json.loads(cites)
    assert isinstance(cites, list)
    assert len(cites) == 1
    assert cites[0]["kind"] == "computed_indicator"
    assert cites[0]["evidence_id"] == ev["evidence_id"]

    # 5. Worker grades the conviction → pass
    from scripts.run_verify_worker import verify_conviction
    res = await verify_conviction({
        "id": row["id"], "agent_name": test_agent, "symbol": "SBUX",
        "direction": "long", "citations": cites,
    })
    assert res["action"] == "pass"
    assert res["citations_ok"] == 1


async def test_a_runner_persists_none_citations_field(test_agent, monkeypatch):
    """ConvictionView without citations → citations column persists as NULL."""
    review_json = json.dumps({
        "views": [{
            "symbol": "SPY", "direction": "long",
            "expected_return_pct": 1.0, "likelihood": 0.5,
            "time_to_target_days": 3, "expires_in_hours": 4,
            "rationale": "no citations",
        }],
        "forecasts": [], "theses_to_record": [], "theses_to_grade": [],
        "telegram_summary": "x",
    })
    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_llm_client([_final_text_response(review_json)]),
    )
    await runner.run_skill(test_agent, "review", dry_run=False)

    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT citations FROM agent_conviction "
            "WHERE agent_name=$1 AND symbol='SPY'", test_agent,
        )
    assert row is not None
    assert row["citations"] is None


# ── (b) from_model auto-citation: model_run citation minted on the row ───────


async def test_b_from_model_mints_model_run_auto_citation(monkeypatch):
    """When a view sets from_model, the runner runs the model AND auto-mints
    a Citation(kind='model_run', source_ref_id=forecast_run_id). That citation
    must land on the persisted row and verify cleanly via the agent_forecast
    existence check.

    Uses agent_name='atlas' directly (not the synthetic test_agent fixture)
    because the model_loader regex `^[a-z]...` rejects test agent names with
    a leading underscore. Manually cleans up the seeded atlas/SBUX rows in
    the finally block to avoid polluting live data.
    """
    AGENT = "atlas"
    SYMBOL = "SBUX"
    SENTINEL_RATIONALE = "from_model_auto_citation_integration_test"

    review_json = json.dumps({
        "views": [{
            "symbol": SYMBOL, "direction": "flat",     # from_model overrides
            "expires_in_hours": 24,
            "rationale": SENTINEL_RATIONALE,
            "from_model": "hmm_regime_mix",
        }],
        "forecasts": [], "theses_to_record": [], "theses_to_grade": [],
        "telegram_summary": "x",
    })
    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_llm_client([_final_text_response(review_json)]),
    )

    from db.schema import get_pool
    pool = await get_pool()

    # Snapshot atlas conviction state before run so we only delete what we made
    async with pool.acquire() as conn:
        pre_rows = await conn.fetch(
            "SELECT id FROM agent_conviction WHERE agent_name=$1", AGENT,
        )
    pre_ids = {r["id"] for r in pre_rows}

    try:
        result = await runner.run_skill(AGENT, "review", dry_run=False)
        if result.write_summary["views_inserted"] == 0:
            pytest.skip(f"from_model declined for {SYMBOL}: {result.write_summary}")

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, citations, forecast_run_id FROM agent_conviction "
                "WHERE agent_name=$1 AND symbol=$2 AND rationale=$3",
                AGENT, SYMBOL, SENTINEL_RATIONALE,
            )
        assert row is not None, "atlas/SBUX row not found post-run"
        cites = row["citations"]
        if isinstance(cites, str):
            cites = json.loads(cites)
        assert isinstance(cites, list) and len(cites) >= 1, (
            f"expected at least one auto-citation, got {cites!r}"
        )

        model_run_cites = [c for c in cites if c.get("kind") == "model_run"]
        assert len(model_run_cites) == 1
        auto = model_run_cites[0]
        assert auto["evidence_id"] == 0   # placeholder per design
        assert auto["source_ref_id"] == str(row["forecast_run_id"])

        # The model_run citation should pass via the forecast_run_id existence check.
        from scripts.run_verify_worker import verify_conviction
        res = await verify_conviction({
            "id": row["id"], "agent_name": AGENT, "symbol": SYMBOL,
            "direction": "long", "citations": cites,
        })
        assert res["action"] == "pass", f"expected pass, got {res}"
        assert res["citations_ok"] == len(cites)
    finally:
        # Clean up anything this test added to atlas's live conviction state.
        async with pool.acquire() as conn:
            await conn.execute(
                """DELETE FROM conviction_verification WHERE conviction_id IN (
                     SELECT id FROM agent_conviction
                     WHERE agent_name=$1 AND rationale=$2
                       AND id <> ALL($3::bigint[])
                   )""",
                AGENT, SENTINEL_RATIONALE, list(pre_ids) or [0],
            )
            await conn.execute(
                """DELETE FROM agent_conviction
                   WHERE agent_name=$1 AND rationale=$2
                     AND id <> ALL($3::bigint[])""",
                AGENT, SENTINEL_RATIONALE, list(pre_ids) or [0],
            )


async def test_b_model_run_citation_rejects_missing_forecast_run_id(test_agent):
    """Direct pipeline test: a citation with kind='model_run' and a non-existent
    forecast_run_id should be flagged. Tests the ModelRunPipeline directly
    (refactored 2026-05-21 to live in meta_agent.citation_pipeline)."""
    from meta_agent.citation_pipeline import get_pipeline
    # Synthesize a UUID that won't be in agent_forecast
    bogus_uuid = "11111111-1111-4111-8111-111111111111"
    citation = {
        "kind": "model_run",
        "evidence_id": 0,
        "source_ref_id": bogus_uuid,
        "quote": "bogus",
    }
    res = await get_pipeline("model_run").verify(citation)
    assert res.ok is False
    assert "not in agent_forecast" in res.reason


# ── (c) MCP tool surface: the registered @mcp.tool() wrappers are callable ──


async def test_c_mcp_compute_indicator_returns_valid_json():
    """The MCP wrapper of compute_indicator round-trips through json.dumps
    and returns the same shape the agent sees on the wire."""
    import mcp_server
    raw = await mcp_server.compute_indicator(
        symbol="SBUX", indicator="RSI_14",
        agent_name="mcp-surface-test",
    )
    assert isinstance(raw, str)
    out = json.loads(raw)
    assert out["ok"] is True
    assert out["symbol"] == "SBUX"
    assert out["indicator"] == "RSI_14"
    assert "evidence_id" in out and out["evidence_id"] > 0


async def test_c_mcp_query_news_returns_evidence_id():
    """query_news through the MCP wrapper still stamps evidence."""
    import mcp_server
    raw = await mcp_server.query_news(
        terms=["earnings"], window_days=3, agent_name="mcp-surface-test",
    )
    out = json.loads(raw)
    assert out["ok"] is True
    assert "evidence_id" in out


async def test_c_mcp_verify_catalyst_returns_confidence_tier():
    import mcp_server
    raw = await mcp_server.verify_catalyst(
        event_text="completely_invented_catalyst_for_mcp_surface_test",
        date="2026-06-15", agent_name="mcp-surface-test",
    )
    out = json.loads(raw)
    assert out["ok"] is True
    # No news will match → confidence should be 'absent'
    assert out["confidence"] == "absent"
    assert out["found"] is False


async def test_c_mcp_run_skill_dispatches_to_registry():
    import mcp_server
    raw = await mcp_server.run_skill(
        agent_name="energy", skill_name="compute_above_sma200",
        args={"symbol": "SBUX"}, session_id="mcp-surface-test",
    )
    out = json.loads(raw)
    assert out["status"] == "ok"
    assert "evidence_id" in out["payload"]


async def test_c_mcp_run_skill_rejects_unknown_skill():
    import mcp_server
    raw = await mcp_server.run_skill(
        agent_name="energy", skill_name="never_authored_skill",
    )
    out = json.loads(raw)
    assert out["status"] == "error"
    assert "not found" in out["error"]


# ── (f) Skill → Citation → runner → verifier (full round-trip) ──────────────


async def test_f_skill_evidence_id_rounds_trips_to_verified_citation(
    test_agent, monkeypatch,
):
    """The full Phase B+C flow: agent calls run_skill, gets an evidence_id,
    builds a Citation pointing at it, submits a ConvictionView via the
    runner, then the verifier grades it pass."""
    from meta_agent.skill_loader import run_skill

    # 1. Agent runs a skill — gets evidence_id
    skill_res = await run_skill(
        agent_name="atlas",  # any agent that has the symlinked skill works
        skill_name="compute_above_sma200",
        args={"symbol": "SBUX"},
        session_id="phase-f-test",
    )
    assert skill_res["status"] == "ok"
    payload = skill_res["payload"]
    assert payload["ok"] is True
    ev_id = payload["evidence_id"]
    inputs = payload["inputs_used"]

    # 2. Agent emits ConvictionView with a Citation pointing at the skill output
    review_json = json.dumps({
        "views": [{
            "symbol": "SBUX", "direction": "long",
            "expected_return_pct": 1.5, "likelihood": 0.55,
            "time_to_target_days": 5, "expires_in_hours": 24,
            "rationale": "SBUX trades above its 200-day SMA per the skill.",
            "citations": [{
                "kind": "computed_indicator",
                "evidence_id": ev_id,
                "source_ref_id": f"SBUX:ABOVE_SMA200:{inputs['asof']}",
                "quote": f"SBUX above SMA200: {payload['result']}",
            }],
        }],
        "forecasts": [], "theses_to_record": [], "theses_to_grade": [],
        "telegram_summary": "x",
    })
    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_llm_client([_final_text_response(review_json)]),
    )
    await runner.run_skill(test_agent, "review", dry_run=False)

    # 3. Fetch the persisted row + cited evidence
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, citations FROM agent_conviction "
            "WHERE agent_name=$1 AND symbol='SBUX'", test_agent,
        )
    assert row is not None
    cites = row["citations"]
    if isinstance(cites, str):
        cites = json.loads(cites)
    assert any(c["evidence_id"] == ev_id for c in cites)

    # 4. Worker grades it → pass
    from scripts.run_verify_worker import verify_conviction
    res = await verify_conviction({
        "id": row["id"], "agent_name": test_agent, "symbol": "SBUX",
        "direction": "long", "citations": cites,
    })
    assert res["action"] == "pass"
    assert res["citations_ok"] == len(cites)
