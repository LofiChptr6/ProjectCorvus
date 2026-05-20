"""Phase 2 — review pipeline.

Covers:
  - ReviewOutput pydantic validation (unit)
  - _parse_structured handles plain JSON, code-fenced JSON, malformed JSON
  - Bundler degrades gracefully without crashing
  - Full pipeline with mocked LLM in dry-run mode → shadow tables populated
  - Live mode → real conviction/forecast/thesis tables populated
  - Validation failure triggers retry, then writes still happen on retry success
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import pytest

from db import store
from db.schema import get_pool
from pipelines import llm_client, runner, schemas


# ── Fake OpenAI plumbing (lifted from test_respond_pipeline) ─────────────────


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
    def __init__(self, scripted: list[_FakeResponse]):
        self._scripted = list(scripted)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._scripted:
            raise RuntimeError("scripted responses exhausted")
        return self._scripted.pop(0)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions):
        self.completions = completions


class _FakeOpenAI:
    def __init__(self, scripted: list[_FakeResponse]):
        self.chat = _FakeChat(_FakeCompletions(scripted))


def _stub_llm_client(scripted: list[_FakeResponse]) -> llm_client.LLMClient:
    return llm_client.LLMClient(
        client=_FakeOpenAI(scripted),
        base_url="http://fake/v1",
        session_id="test-review-sess",
        model="fake-model",
    )


def _final_text_response(text: str) -> _FakeResponse:
    return _FakeResponse(choices=[_FakeChoice(
        finish_reason="stop",
        message=_FakeMessage(content=text),
    )])


# ── Pydantic schema unit tests ────────────────────────────────────────────────


def test_review_output_minimal_valid():
    parsed = schemas.ReviewOutput.model_validate({})
    assert parsed.views == []
    assert parsed.forecasts == []


def test_review_output_full_valid():
    payload = {
        "views": [{
            "symbol": "spy",  # lowercase; validator should upper
            "direction": "long",
            "expected_return_pct": 1.2,
            "likelihood": 0.7,
            "time_to_target_days": 3,
            "rationale": "trend intact",
            "expires_in_hours": 4,
        }],
        "forecasts": [{
            "symbol": "tlt",
            "expected_return_pct": -0.5,
            "likelihood": 0.6,
            "time_to_target_days": 2,
            "method": "regime",
            "expires_in_hours": 2,
        }],
        "theses_to_record": [{
            "kind": "prediction",
            "title": "SPY > 700 in 3d",
            "body": "Trend + breadth supportive.",
            "verify_by": "2026-05-15",
        }],
        "telegram_summary": "atlas: 1 long, 1 forecast",
    }
    parsed = schemas.ReviewOutput.model_validate(payload)
    assert parsed.views[0].symbol == "SPY"  # upper
    assert parsed.forecasts[0].symbol == "TLT"


def test_conviction_view_rejects_short_direction():
    with pytest.raises(Exception):
        schemas.ConvictionView.model_validate({
            "symbol": "SPY", "direction": "short",
            "expected_return_pct": -1.0, "likelihood": 0.5,
            "time_to_target_days": 3, "expires_in_hours": 4,
        })


def _valid_long_payload(**overrides) -> dict:
    """Build a clean ConvictionView dict in the new contract — caller can
    override any field for the specific assertion."""
    base = {
        "symbol": "SPY", "direction": "long",
        "expected_return_pct": 1.0, "likelihood": 0.5,
        "time_to_target_days": 3, "expires_in_hours": 4,
    }
    base.update(overrides)
    return base


def test_conviction_view_coerces_negative_stop_pct():
    """LLMs emit stop_pct as a signed number (-0.05 = 5% loss limit). Our
    convention is positive magnitude. Take abs() rather than reject."""
    parsed = schemas.ConvictionView.model_validate(
        _valid_long_payload(stop_pct=-0.05),
    )
    assert parsed.stop_pct == 0.05


def test_conviction_view_preserves_positive_stop_pct():
    parsed = schemas.ConvictionView.model_validate(
        _valid_long_payload(stop_pct=0.07),
    )
    assert parsed.stop_pct == 0.07


def test_conviction_view_stop_pct_none_passes_through():
    parsed = schemas.ConvictionView.model_validate(_valid_long_payload())
    assert parsed.stop_pct is None


def test_conviction_view_rejects_out_of_range_likelihood():
    """likelihood is the bounded confidence field on the new contract;
    [0, 1] is enforced by pydantic's Field(ge=0, le=1)."""
    with pytest.raises(Exception):
        schemas.ConvictionView.model_validate(_valid_long_payload(likelihood=1.5))


def test_conviction_view_rejects_long_with_negative_er():
    """Sign discipline: long requires expected_return_pct >= 0. Bearish views
    route via direction='long' on the inverse ETF instead."""
    with pytest.raises(Exception):
        schemas.ConvictionView.model_validate(
            _valid_long_payload(expected_return_pct=-2.0),
        )


def test_conviction_view_rejects_long_missing_likelihood():
    with pytest.raises(Exception):
        schemas.ConvictionView.model_validate({
            "symbol": "SPY", "direction": "long",
            "expected_return_pct": 1.0, "time_to_target_days": 3,
            "expires_in_hours": 4,
        })


def test_thesis_record_rejects_unknown_kind():
    with pytest.raises(Exception):
        schemas.ThesisRecord.model_validate({
            "kind": "rumination", "title": "x", "body": "y",
        })


# ── _parse_structured ────────────────────────────────────────────────────────


def test_parse_structured_plain_json():
    parsed, err = runner._parse_structured("review", '{"views": []}')
    assert err is None
    assert isinstance(parsed, schemas.ReviewOutput)


def test_parse_structured_strips_code_fence():
    text = '```json\n{"views": []}\n```'
    parsed, err = runner._parse_structured("review", text)
    assert err is None and parsed is not None


def test_parse_structured_returns_error_on_bad_json():
    parsed, err = runner._parse_structured("review", "{broken")
    assert parsed is None and "json decode" in err


def test_parse_structured_returns_error_on_validation_failure():
    text = '{"views": [{"symbol": "SPY", "direction": "x", "conviction": 0.5}]}'
    parsed, err = runner._parse_structured("review", text)
    assert parsed is None and "validation" in err


def test_strip_code_fence_handles_trailing_fence():
    assert runner._strip_code_fence("```\n{}\n```") == "{}"


def test_strip_code_fence_strips_qwen_thinking_block():
    text = '<think>Let me think about this carefully…</think>\n{"views": []}'
    assert runner._strip_code_fence(text) == '{"views": []}'


def test_strip_code_fence_extracts_json_from_prose():
    text = "Here's my analysis:\n\n{\"views\": []}\n\nLet me know if you need anything else."
    assert runner._strip_code_fence(text) == '{"views": []}'


def test_strip_code_fence_handles_thinking_plus_fence():
    text = '<think>plan</think>\n```json\n{"views": []}\n```'
    assert runner._strip_code_fence(text) == '{"views": []}'


# ── Bundler smoke (no IBKR live) ─────────────────────────────────────────────


async def test_review_bundle_degrades_gracefully(test_agent):
    """Test agent doesn't have an agents/ folder; bundler should still produce a usable bundle."""
    from agent.bundlers.review import get_review_bundle
    bundle = await get_review_bundle(test_agent)
    # No agents/<test_agent>/ folder → workspace empty but not None.
    assert bundle.agent_name == test_agent
    assert isinstance(bundle.universe, list)
    # Active views should be an empty list (no convictions for a fresh agent).
    assert bundle.active_views == []
    # Warnings collected for missing context but bundle is usable.
    assert isinstance(bundle.bundle_warnings, list)


# ── Full pipeline (mocked LLM) ───────────────────────────────────────────────


def _review_json_for(test_agent: str) -> str:
    # New contract: agent emits the forecast triple (er, lk, ttd); the runner
    # computes conviction = abs(er) × lk / ttd. For SPY here:
    # 0.8 × 0.6 / 2 = 0.24 — see test_review_dry_run_writes_to_shadow_tables.
    return json.dumps({
        "views": [
            {"symbol": "SPY", "direction": "long",
             "expected_return_pct": 0.8, "likelihood": 0.6,
             "time_to_target_days": 2,
             "rationale": "trend intact", "expires_in_hours": 4},
            {"symbol": "TLT", "direction": "flat",
             "rationale": "no edge", "expires_in_hours": 4},
        ],
        "forecasts": [
            {"symbol": "SPY", "expected_return_pct": 0.8, "likelihood": 0.6,
             "time_to_target_days": 2, "method": "regime", "expires_in_hours": 2},
            {"symbol": "QQQ", "expected_return_pct": 1.0, "likelihood": 0.5,
             "time_to_target_days": 5, "method": "momentum", "horizon": "near",
             "expires_in_hours": 24},
        ],
        "theses_to_record": [
            {"kind": "prediction", "title": "SPY > 700", "body": "details",
             "verify_by": "2026-05-15"},
        ],
        "theses_to_grade": [],
        "telegram_summary": f"{test_agent}: 1 long 1 flat 2 forecasts",
    })


async def test_review_dry_run_writes_to_shadow_tables(test_agent, monkeypatch):
    scripted = [_final_text_response(_review_json_for(test_agent))]
    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_llm_client(scripted),
    )

    result = await runner.run_skill(test_agent, "review", dry_run=True)

    assert result.parsed_output is not None
    assert result.write_summary is not None
    assert result.write_summary["dry_run"] is True
    assert result.write_summary["views_inserted"] == 2
    assert result.write_summary["forecasts_inserted"] == 2

    # Real shadow rows.
    pool = await get_pool()
    async with pool.acquire() as conn:
        n_conv = await conn.fetchval(
            "SELECT count(*) FROM agent_conviction_shadow WHERE agent_name=$1", test_agent,
        )
        n_fc = await conn.fetchval(
            "SELECT count(*) FROM agent_forecast_shadow WHERE agent_name=$1", test_agent,
        )
        # Live tables should NOT have any rows for this agent (dry-run).
        n_live_conv = await conn.fetchval(
            "SELECT count(*) FROM agent_conviction WHERE agent_name=$1", test_agent,
        )
    assert n_conv == 2
    assert n_fc == 2
    assert n_live_conv == 0


async def test_review_live_writes_to_real_tables(test_agent, monkeypatch):
    scripted = [_final_text_response(_review_json_for(test_agent))]
    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_llm_client(scripted),
    )

    result = await runner.run_skill(test_agent, "review", dry_run=False)
    assert result.write_summary["dry_run"] is False
    assert result.write_summary["views_inserted"] == 2
    assert result.write_summary["forecasts_inserted"] == 2
    assert result.write_summary["theses_recorded"] == 1

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT symbol, direction, conviction FROM agent_conviction WHERE agent_name=$1 ORDER BY symbol",
            test_agent,
        )
    syms = {r["symbol"]: (r["direction"], float(r["conviction"])) for r in rows}
    # SPY: central calc = abs(0.8) * 0.6 / 2 = 0.24. TLT: flat → 0.0.
    assert syms == {"SPY": ("long", 0.24), "TLT": ("flat", 0.0)}


async def test_review_replaces_prior_convictions(test_agent, monkeypatch):
    """Each review run should clear and re-populate — no stale views carry forward."""
    # Pre-existing live row.
    await store.upsert_conviction(
        agent_name=test_agent, symbol="OLD", direction="long",
        conviction=0.9, expires_in_hours=24,
    )

    scripted = [_final_text_response(_review_json_for(test_agent))]
    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_llm_client(scripted),
    )
    await runner.run_skill(test_agent, "review", dry_run=False)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT symbol FROM agent_conviction WHERE agent_name=$1", test_agent,
        )
    syms = {r["symbol"] for r in rows}
    assert "OLD" not in syms
    assert syms == {"SPY", "TLT"}


async def test_review_invalid_json_triggers_retry(test_agent, monkeypatch):
    """First response is malformed JSON → runner retries with error feedback;
    second response is valid → writes happen."""
    scripted = [
        _final_text_response("not json at all"),
        _final_text_response(_review_json_for(test_agent)),
    ]
    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_llm_client(scripted),
    )

    result = await runner.run_skill(test_agent, "review", dry_run=True)
    assert len(result.validation_errors) == 1
    assert result.parsed_output is not None
    assert result.write_summary["views_inserted"] == 2


async def test_review_double_failure_skips_writes(test_agent, monkeypatch):
    """Both first and retry are malformed → no writes happen, errors recorded."""
    scripted = [
        _final_text_response("garbage"),
        _final_text_response("still garbage"),
    ]
    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_llm_client(scripted),
    )

    result = await runner.run_skill(test_agent, "review", dry_run=True)
    assert result.parsed_output is None
    assert result.write_summary is None
    assert len(result.validation_errors) == 2
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM agent_conviction_shadow WHERE agent_name=$1", test_agent,
        )
    assert n == 0
