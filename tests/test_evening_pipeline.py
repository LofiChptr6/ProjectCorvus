"""Phase 3 — evening pipeline.

Single-shot path: bundle → template → LLM (no tool loop) → structured-output
→ digest write + slide/telegram side effects (mocked in tests).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import pytest

from db import store
from db.schema import get_pool
from pipelines import llm_client, runner, schemas


# ── Fake OpenAI plumbing ──────────────────────────────────────────────────────


@dataclass
class _FakeMessage:
    content: Optional[str]
    tool_calls: Optional[list] = None


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
    def __init__(self, scripted): self._scripted = list(scripted)
    async def create(self, **_):
        if not self._scripted: raise RuntimeError("no scripted")
        return self._scripted.pop(0)


class _FakeChat:
    def __init__(self, completions): self.completions = completions


class _FakeOpenAI:
    def __init__(self, scripted): self.chat = _FakeChat(_FakeCompletions(scripted))


def _stub_client(scripted):
    return llm_client.LLMClient(
        client=_FakeOpenAI(scripted),
        base_url="http://fake/v1",
        session_id="evening-test",
        model="fake-model",
    )


def _final(text):
    return _FakeResponse(choices=[_FakeChoice("stop", _FakeMessage(text))])


# ── Schema unit tests ────────────────────────────────────────────────────────


def test_evening_output_minimal_valid():
    parsed = schemas.EveningOutput.model_validate({"headline": "P&L: +$200"})
    assert parsed.headline == "P&L: +$200"
    assert parsed.trends == []


def test_evening_output_full_valid():
    payload = {
        "headline": "P&L: +$200 today",
        "trends": ["t1", "t2"],
        "theses": ["thesis1"],
        "philosophy": ["p1"],
        "open_questions": ["q1"],
        "pnl_today": 200.0,
        "pnl_week": 850.0,
        "telegram_caption": "ATLAS | EOD",
        "theses_to_record": [{
            "kind": "observation", "title": "Yields broke", "body": "TLT-90",
        }],
        "theses_to_grade": [],
    }
    parsed = schemas.EveningOutput.model_validate(payload)
    assert parsed.pnl_today == 200.0
    assert len(parsed.theses_to_record) == 1


def test_evening_output_requires_headline():
    with pytest.raises(Exception):
        schemas.EveningOutput.model_validate({"trends": ["x"]})


# ── Bundler ──────────────────────────────────────────────────────────────────


async def test_evening_bundle_degrades_gracefully(test_agent):
    from agent.bundlers.evening import get_evening_bundle
    bundle = await get_evening_bundle(test_agent)
    assert bundle.agent_name == test_agent
    assert bundle.trading_date_iso  # ISO date
    assert bundle.active_views == []
    assert isinstance(bundle.journal_open, list)


# ── Full pipeline (mocked LLM, mocked side effects) ──────────────────────────


def _evening_json(test_agent: str) -> str:
    return json.dumps({
        "headline": f"P&L: +$120 today ({test_agent})",
        "trends": ["sector quiet but breadth +"],
        "theses": ["regime intact"],
        "philosophy": ["sized 0.6 max conviction"],
        "open_questions": ["FOMC tomorrow"],
        "pnl_today": 120.0,
        "pnl_week": 480.0,
        "telegram_caption": f"{test_agent.upper()} | TEST | EOD",
        "theses_to_record": [{
            "kind": "prediction", "title": "Tomorrow up day",
            "body": "breadth + reflexive bid", "verify_by": "2026-05-12",
        }],
        "theses_to_grade": [],
    })


async def test_evening_full_pipeline_writes_digest(test_agent, monkeypatch):
    scripted = [_final(_evening_json(test_agent))]
    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_client(scripted),
    )

    # Stub slide + telegram so we don't render real images / hit user's chat.
    from pipelines import notify, runner_evening
    async def _no_slide(*a, **kw): return None
    async def _no_chart(*a, **kw): return False
    monkeypatch.setattr(runner_evening, "_generate_slide_safe", _no_slide)
    monkeypatch.setattr(notify, "send_chart_safe", _no_chart)

    result = await runner.run_skill(test_agent, "evening", dry_run=False)

    assert result.parsed_output is not None
    assert result.write_summary is not None
    assert result.write_summary["digest_id"] is not None
    assert result.write_summary["theses_recorded"] == 1

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM agent_evening_digests WHERE agent_name=$1 ORDER BY id DESC LIMIT 1",
            test_agent,
        )
    assert row is not None
    assert float(row["pnl_today"]) == 120.0


async def test_evening_dry_run_does_everything_live_does(test_agent, monkeypatch):
    """New contract: dry-run is full live pipeline. Slide generation, telegram,
    digest, theses ALL fire — caption gets `[DRY-RUN] ` prepended so the user
    can tell at a glance."""
    scripted = [_final(_evening_json(test_agent))]
    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_client(scripted),
    )
    slide_calls: list[dict] = []
    chart_calls: list[dict] = []
    from pipelines import notify, runner_evening
    async def _stub_slide(agent_name, output): slide_calls.append({"agent": agent_name}); return "/tmp/fake.png"
    async def _stub_chart(image_path, caption, *, dry_run=False, **kwargs):
        chart_calls.append({"caption": caption, "dry_run": dry_run, **kwargs})
        return True
    monkeypatch.setattr(runner_evening, "_generate_slide_safe", _stub_slide)
    monkeypatch.setattr(notify, "send_chart_safe", _stub_chart)

    result = await runner.run_skill(test_agent, "evening", dry_run=True)
    assert result.write_summary["dry_run"] is True
    assert result.write_summary["theses_recorded"] == 1  # NOT skipped any more
    assert result.write_summary["digest_id"] is not None
    assert len(slide_calls) == 1
    assert len(chart_calls) == 1 and chart_calls[0]["dry_run"] is True
    assert result.write_summary["telegram_sent"] is True
    assert result.write_summary["chart_path"] == "/tmp/fake.png"


async def test_evening_invalid_json_triggers_retry(test_agent, monkeypatch):
    scripted = [
        _final("not json"),
        _final(_evening_json(test_agent)),
    ]
    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_client(scripted),
    )
    from pipelines import notify, runner_evening
    async def _no_slide(*a, **kw): return None
    async def _no_chart(*a, **kw): return False
    monkeypatch.setattr(runner_evening, "_generate_slide_safe", _no_slide)
    monkeypatch.setattr(notify, "send_chart_safe", _no_chart)

    result = await runner.run_skill(test_agent, "evening", dry_run=False)
    assert len(result.validation_errors) == 1
    assert result.parsed_output is not None
    assert result.write_summary["digest_id"] is not None
