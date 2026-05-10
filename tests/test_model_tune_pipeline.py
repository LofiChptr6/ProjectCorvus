"""Phase 4 — model-tune pipeline integration.

Mocked LLM emits ModelTuneOutput JSON with file actions; runner_model_tune
applies file_ritual and writes hypothesis log + thesis. Shadow mode redirects
file actions to .shadow subdir.
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
from pipelines import file_ritual, llm_client, runner, runner_model_tune, schemas


# ── Fake OpenAI ───────────────────────────────────────────────────────────────


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
        session_id="model-tune-test",
        model="fake-model",
    )


def _final(text):
    return _FakeResponse(choices=[_FakeChoice("stop", _FakeMessage(text))])


# ── Schema unit tests ────────────────────────────────────────────────────────


def test_model_tune_output_minimal_valid():
    payload = {
        "file_actions": [],
        "hypothesis_log_update": "# log\n",
        "thesis": {"kind": "observation", "title": "x", "body": "y"},
        "telegram_summary": "🔬 done",
    }
    parsed = schemas.ModelTuneOutput.model_validate(payload)
    assert parsed.file_actions == []


def test_model_file_action_rejects_path_escape():
    with pytest.raises(Exception):
        schemas.ModelFileAction.model_validate({
            "action": "add", "file_path": "../etc/passwd", "reason": "x",
        })


def test_model_file_action_rejects_absolute_path():
    with pytest.raises(Exception):
        schemas.ModelFileAction.model_validate({
            "action": "add", "file_path": "/etc/passwd", "reason": "x",
        })


def test_model_tune_output_caps_actions_at_two():
    payload = {
        "file_actions": [
            {"action": "tune", "file_path": "agents/a/models/x.py", "new_content": "y", "new_version": "1.0", "reason": "z"},
            {"action": "tune", "file_path": "agents/a/models/y.py", "new_content": "y", "new_version": "1.0", "reason": "z"},
            {"action": "tune", "file_path": "agents/a/models/z.py", "new_content": "y", "new_version": "1.0", "reason": "z"},
        ],
        "hypothesis_log_update": "x",
        "thesis": {"kind": "observation", "title": "x", "body": "y"},
        "telegram_summary": "x",
    }
    with pytest.raises(Exception):
        schemas.ModelTuneOutput.model_validate(payload)


# ── apply_model_tune_output (write path, no LLM) ─────────────────────────────


GOOD_MODEL = '''"""test."""
MODEL_VERSION = "1.0"

def compute(symbol, bars, context):
    return {
        "direction": "flat", "conviction": 0.0,
        "expected_return_pct": 0.0, "time_to_target_days": 1, "inputs": {},
    }
'''


def _setup_repo_for_tune(tmp_path: Path) -> tuple[Path, str, str]:
    """Returns (repo_root, prefix, sector). Adds repo_root to sys.path."""
    prefix = f"ritual_runner_{uuid.uuid4().hex[:6]}"
    sector = "atlas"
    (tmp_path / prefix / sector / "models").mkdir(parents=True)
    (tmp_path / prefix / sector / "notes").mkdir(parents=True)
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))
    return tmp_path, prefix, sector


def _patch_paths_for_tune_test(monkeypatch, prefix: str, sector: str, tmp_path: Path):
    """runner_model_tune hardcodes 'agents/<sector>/models' — for tests we need
    to point it at our tmp prefix instead. Monkeypatch the path helpers."""
    import pipelines.runner_model_tune as rmt

    def _models_root(agent_name, dry_run, repo_root):
        if dry_run:
            return repo_root / prefix / agent_name / "models_shadow"
        return repo_root / prefix / agent_name / "models"

    def _hyp_path(agent_name, dry_run, repo_root):
        base = repo_root / prefix / agent_name / "notes"
        name = "model_hypothesis.shadow.md" if dry_run else "model_hypothesis.md"
        return base / name

    monkeypatch.setattr(rmt, "_models_root", _models_root)
    monkeypatch.setattr(rmt, "_hypothesis_log_path", _hyp_path)


async def test_apply_tune_output_live_writes_file(tmp_path, test_agent, monkeypatch):
    repo_root, prefix, sector = _setup_repo_for_tune(tmp_path)
    _patch_paths_for_tune_test(monkeypatch, prefix, sector, tmp_path)

    # Build a ModelTuneOutput with one ADD action.
    payload = schemas.ModelTuneOutput.model_validate({
        "file_actions": [{
            "action": "add",
            "file_path": f"{prefix}/{sector}/models/regime.py",
            "new_content": GOOD_MODEL, "new_version": "1.0",
            "reason": "first model in portfolio",
        }],
        "hypothesis_log_update": "# log\n## first run\n",
        "thesis": {
            "kind": "observation", "title": "model_tune: bootstrap",
            "body": "added regime.py", "verify_by": "2026-05-17",
        },
        "telegram_summary": "🔬 atlas — added regime.py",
    })

    result = await runner_model_tune.apply_model_tune_output(
        payload, agent_name=sector, dry_run=False,
        session_id="rmt-test", repo_root=repo_root,
    )
    assert result["actions_ok"] == 1
    assert result["actions_failed"] == 0
    assert (tmp_path / prefix / sector / "models" / "regime.py").exists()
    assert (tmp_path / prefix / sector / "notes" / "model_hypothesis.md").exists()
    assert result["thesis_id"] is not None

    # Cleanup the recorded thesis (live writes hit the real DB).
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM agent_thesis WHERE agent_name=$1 AND title LIKE 'model_tune:%'", sector)


async def test_apply_tune_output_dry_run_writes_to_shadow(tmp_path, monkeypatch):
    repo_root, prefix, sector = _setup_repo_for_tune(tmp_path)
    _patch_paths_for_tune_test(monkeypatch, prefix, sector, tmp_path)

    payload = schemas.ModelTuneOutput.model_validate({
        "file_actions": [{
            "action": "add",
            "file_path": f"{prefix}/{sector}/models/shadowtest.py",
            "new_content": GOOD_MODEL, "new_version": "1.0",
            "reason": "bootstrap shadow",
        }],
        "hypothesis_log_update": "# shadow log\n",
        "thesis": {
            "kind": "observation", "title": "model_tune: shadow",
            "body": "shadow test", "verify_by": "2026-05-17",
        },
        "telegram_summary": "🔬 shadow",
    })

    result = await runner_model_tune.apply_model_tune_output(
        payload, agent_name=sector, dry_run=True,
        session_id="shadow-test", repo_root=repo_root,
    )
    assert result["dry_run"] is True
    # File goes to models_shadow/, NOT the live models dir.
    assert (tmp_path / prefix / sector / "models_shadow" / "shadowtest.py").exists()
    assert not (tmp_path / prefix / sector / "models" / "shadowtest.py").exists()
    # Hypothesis log written to .shadow.md not the live one.
    assert (tmp_path / prefix / sector / "notes" / "model_hypothesis.shadow.md").exists()

    # Cleanup the thesis.
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM agent_thesis WHERE agent_name=$1 AND title LIKE 'model_tune:%'", sector)


async def test_apply_tune_broken_content_records_failure(tmp_path, monkeypatch):
    repo_root, prefix, sector = _setup_repo_for_tune(tmp_path)
    _patch_paths_for_tune_test(monkeypatch, prefix, sector, tmp_path)

    payload = schemas.ModelTuneOutput.model_validate({
        "file_actions": [{
            "action": "add",
            "file_path": f"{prefix}/{sector}/models/broken.py",
            "new_content": 'MODEL_VERSION = "1.0"\ndef compute(  # broken syntax\n',
            "new_version": "1.0",
            "reason": "test rollback",
        }],
        "hypothesis_log_update": "# log\n",
        "thesis": {"kind": "observation", "title": "model_tune: broken", "body": "x", "verify_by": "2026-05-17"},
        "telegram_summary": "🔬",
    })

    result = await runner_model_tune.apply_model_tune_output(
        payload, agent_name=sector, dry_run=True,
        session_id="broken-test", repo_root=repo_root,
    )
    assert result["actions_ok"] == 0
    assert result["actions_failed"] == 1
    assert result["actions"][0]["stage"] == "import"
    assert result["actions"][0]["restored"] is True
    # Broken file should have been deleted.
    assert not (tmp_path / prefix / sector / "models_shadow" / "broken.py").exists()

    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM agent_thesis WHERE agent_name=$1 AND title LIKE 'model_tune:%'", sector)
