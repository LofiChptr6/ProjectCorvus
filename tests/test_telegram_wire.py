"""Verify review + model_tune fire Telegram in live mode, stay silent in dry-run.

All tests monkey-patch `approval.telegram.send_message` to a no-op recorder so
nothing actually hits the user's Telegram chat. The check is: was the right
text dispatched in the right mode?
"""
from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest

from db import store
from pipelines import llm_client, notify, runner, runner_model_tune, schemas


# ── Recorder ──────────────────────────────────────────────────────────────────


class _TelegramRecorder:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []
    async def __call__(self, text: str, parse_mode: Optional[str] = "Markdown",
                       reply_markup: Optional[dict] = None, **kwargs: Any) -> Optional[dict]:
        # kwargs absorbs the new kind/role/meta keyword-only args on send_message.
        # Recorders that care can inspect kwargs; existing tests only check text/parse_mode.
        self.calls.append({"text": text, "parse_mode": parse_mode, **kwargs})
        return {"ok": True}


@pytest.fixture
def telegram_recorder(monkeypatch):
    recorder = _TelegramRecorder()
    import approval.telegram
    monkeypatch.setattr(approval.telegram, "send_message", recorder)
    return recorder


# ── notify unit tests ────────────────────────────────────────────────────────


async def test_send_summary_safe_empty_is_noop(telegram_recorder):
    assert await notify.send_summary_safe("atlas", "") is False
    assert await notify.send_summary_safe("atlas", None) is False
    assert telegram_recorder.calls == []


async def test_send_summary_safe_prefixes_agent_name(telegram_recorder):
    ok = await notify.send_summary_safe("atlas", "regime stable; long SPY 0.65")
    assert ok is True
    assert len(telegram_recorder.calls) == 1
    assert telegram_recorder.calls[0]["text"] == "*atlas*: regime stable; long SPY 0.65"


async def test_send_summary_safe_swallows_exceptions(monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("network down")
    import approval.telegram
    monkeypatch.setattr(approval.telegram, "send_message", boom)
    assert await notify.send_summary_safe("atlas", "hi") is False


async def test_send_summary_safe_dry_run_prepends_prefix(telegram_recorder):
    ok = await notify.send_summary_safe("atlas", "regime stable", dry_run=True)
    assert ok is True
    assert telegram_recorder.calls[0]["text"] == "[DRY-RUN] *atlas*: regime stable"


async def test_send_chart_safe_dry_run_prepends_prefix(monkeypatch):
    captured: list[dict] = []
    async def fake_chart(image_path, caption, **kwargs):
        captured.append({"image_path": image_path, "caption": caption, **kwargs})
        return {"ok": True}
    import approval.telegram
    monkeypatch.setattr(approval.telegram, "send_photo", fake_chart)
    ok = await notify.send_chart_safe("/tmp/x.png", "ATLAS | EOD", dry_run=True)
    assert ok is True
    assert captured[0]["caption"] == "[DRY-RUN] ATLAS | EOD"


async def test_send_chart_safe_live_no_prefix(monkeypatch):
    captured: list[dict] = []
    async def fake_chart(image_path, caption, **kwargs):
        captured.append({"image_path": image_path, "caption": caption, **kwargs})
        return {"ok": True}
    import approval.telegram
    monkeypatch.setattr(approval.telegram, "send_photo", fake_chart)
    ok = await notify.send_chart_safe("/tmp/x.png", "ATLAS | EOD", dry_run=False)
    assert ok is True
    assert captured[0]["caption"] == "ATLAS | EOD"


# ── Fake OpenAI (mirrored from other test files) ─────────────────────────────


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
    prompt_tokens: int = 0; completion_tokens: int = 0; total_tokens: int = 0
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
    def __init__(self, c): self.completions = c


class _FakeOpenAI:
    def __init__(self, scripted): self.chat = _FakeChat(_FakeCompletions(scripted))


def _stub_client(scripted):
    return llm_client.LLMClient(
        client=_FakeOpenAI(scripted),
        base_url="http://fake/v1",
        session_id="tg-test",
        model="fake-model",
    )


def _final(text: str) -> _FakeResponse:
    return _FakeResponse(choices=[_FakeChoice("stop", _FakeMessage(text))])


# ── Review: live fires Telegram, dry-run is silent ───────────────────────────


def _review_json_with_summary(summary: str = "regime intact; long SPY") -> str:
    return json.dumps({
        "views": [{"symbol": "SPY", "direction": "long",
                         "expected_return_pct": 0.5, "likelihood": 0.6,
                         "time_to_target_days": 2, "expires_in_hours": 4}],
        "forecasts": [{
            "symbol": "SPY", "expected_return_pct": 0.5, "likelihood": 0.6,
            "time_to_target_days": 2, "method": "regime",
            "expires_in_hours": 2,
        }],
        "telegram_summary": summary,
    })


async def test_review_live_fires_telegram(test_agent, monkeypatch, telegram_recorder):
    scripted = [_final(_review_json_with_summary("alpha"))]
    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_client(scripted),
    )
    result = await runner.run_skill(test_agent, "review", dry_run=False)
    assert result.write_summary["telegram_sent"] is True
    assert len(telegram_recorder.calls) == 1
    assert telegram_recorder.calls[0]["text"] == f"*{test_agent}*: alpha"


async def test_review_dry_run_fires_telegram_with_dry_run_prefix(test_agent, monkeypatch, telegram_recorder):
    """New contract: dry-run is full live pipeline. Telegram fires with
    `[DRY-RUN] ` prepended so the user knows it's not real signal."""
    scripted = [_final(_review_json_with_summary("beta"))]
    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_client(scripted),
    )
    result = await runner.run_skill(test_agent, "review", dry_run=True)
    assert result.write_summary["telegram_sent"] is True
    assert len(telegram_recorder.calls) == 1
    assert telegram_recorder.calls[0]["text"] == f"[DRY-RUN] *{test_agent}*: beta"


async def test_review_live_with_empty_summary_does_not_fire(test_agent, monkeypatch, telegram_recorder):
    """LLM may legitimately omit telegram_summary (None) — runner should not send empty."""
    payload = json.dumps({
        "views": [],
        "forecasts": [],
        # no telegram_summary field
    })
    scripted = [_final(payload)]
    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_client(scripted),
    )
    result = await runner.run_skill(test_agent, "review", dry_run=False)
    assert result.write_summary["telegram_sent"] is False
    assert telegram_recorder.calls == []


# ── Model-tune: live fires Telegram, dry-run is silent ───────────────────────


def _model_tune_payload(summary: str = "🔬 atlas — 1 model added") -> schemas.ModelTuneOutput:
    return schemas.ModelTuneOutput.model_validate({
        "file_actions": [],   # empty so file_ritual doesn't touch disk
        "hypothesis_log_update": "# log\n",
        "thesis": {
            "kind": "observation", "title": "model_tune: noop test",
            "body": "test body", "verify_by": "2026-05-17",
        },
        "telegram_summary": summary,
    })


async def test_model_tune_live_fires_telegram(tmp_path, telegram_recorder):
    test_agent_name = f"__tgtune_{uuid.uuid4().hex[:6]}__"
    parsed = _model_tune_payload("model upgraded")
    res = await runner_model_tune.apply_model_tune_output(
        parsed, agent_name=test_agent_name, dry_run=False,
        repo_root=tmp_path,
    )
    assert res["telegram_sent"] is True
    assert telegram_recorder.calls[0]["text"] == f"*{test_agent_name}*: model upgraded"

    # Cleanup the recorded thesis (live writes hit the real DB).
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_thesis WHERE agent_name=$1 AND title LIKE 'model_tune:%'",
            test_agent_name,
        )


async def test_model_tune_dry_run_fires_telegram_with_prefix(tmp_path, telegram_recorder):
    """Dry-run fires the same Telegram as live, just `[DRY-RUN] `-prefixed."""
    test_agent_name = f"__tgtune_{uuid.uuid4().hex[:6]}__"
    parsed = _model_tune_payload("shadow run")
    res = await runner_model_tune.apply_model_tune_output(
        parsed, agent_name=test_agent_name, dry_run=True,
        repo_root=tmp_path,
    )
    assert res["telegram_sent"] is True
    assert len(telegram_recorder.calls) == 1
    assert telegram_recorder.calls[0]["text"] == f"[DRY-RUN] *{test_agent_name}*: shadow run"

    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_thesis WHERE agent_name=$1 AND title LIKE 'model_tune:%'",
            test_agent_name,
        )
