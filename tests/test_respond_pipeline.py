"""Integration test for the respond pipeline.

Real DB, mocked LLM. Verifies end-to-end:
  inbox row → bundler → template → tool-loop with scripted tool_calls →
  mark_inbox_responded dispatch → DB row flips to responded.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from db import store
from pipelines import llm_client, runner


# ── Fake OpenAI plumbing ──────────────────────────────────────────────────────


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
    content: str | None
    tool_calls: list[_FakeToolCall] | None = None


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
    usage: _FakeUsage = None


class _FakeCompletions:
    def __init__(self, scripted: list[_FakeResponse]):
        self._scripted = list(scripted)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._scripted:
            raise RuntimeError("FakeCompletions ran out of scripted responses")
        return self._scripted.pop(0)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions):
        self.completions = completions


class _FakeOpenAI:
    def __init__(self, scripted: list[_FakeResponse]):
        self.chat = _FakeChat(_FakeCompletions(scripted))


def _stub_llm_client(scripted: list[_FakeResponse], session_id: str = "test-sess") -> llm_client.LLMClient:
    return llm_client.LLMClient(
        client=_FakeOpenAI(scripted),
        base_url="http://fake/v1",
        session_id=session_id,
        model="fake-model",
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_empty_inbox_short_circuits(test_agent):
    """Empty inbox → runner exits with skipped=True, no LLM call."""
    result = await runner.run_skill(test_agent, "respond")
    assert result.skipped is True
    assert result.skip_reason == "empty_inbox"
    assert result.iterations == 0


async def test_happy_path_one_question_marks_responded(test_agent, monkeypatch):
    """LLM returns mark_inbox_responded tool call → DB row marked responded."""
    inbox_id = await store.post_to_inbox(test_agent, "what's your read on TLT?")
    reply_text = "TLT broke 90 — bonds rallying on safe-haven flows. I'm not adding here."

    # Iteration 0: LLM emits one tool call to mark_inbox_responded.
    # Iteration 1: LLM emits final assistant text with no tool calls.
    scripted = [
        _FakeResponse(choices=[_FakeChoice(
            finish_reason="tool_calls",
            message=_FakeMessage(content=None, tool_calls=[_FakeToolCall(
                id="tc_1",
                function=_FakeFunction(
                    name="mark_inbox_responded",
                    arguments=json.dumps({
                        "inbox_id": inbox_id,
                        "response_body": reply_text,
                        "agent_name": test_agent,
                    }),
                ),
            )]),
        )]),
        _FakeResponse(choices=[_FakeChoice(
            finish_reason="stop",
            message=_FakeMessage(content="Done."),
        )]),
    ]

    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_llm_client(scripted, session_id=kw.get("session_id") or "test-sess"),
    )

    result = await runner.run_skill(test_agent, "respond")
    assert result.skipped is False
    assert result.finish_reason == "stop"
    assert result.iterations == 1
    assert any(c["name"] == "mark_inbox_responded" for c in result.tool_call_log)

    # DB row should be flipped.
    pending = await store.get_pending_inbox(test_agent)
    assert pending == []

    recent = await store.get_recent_inbox(test_agent)
    assert recent[0]["response_body"] == reply_text
    assert recent[0]["responded_at"] is not None


async def test_max_iter_cap_returns_max_iter_finish(test_agent, monkeypatch):
    """If LLM never stops calling tools, runner caps iterations and returns max_iter."""
    await store.post_to_inbox(test_agent, "Q")

    # LLM keeps requesting get_quote forever → each iteration gets max_iter+1 responses.
    def _looping_response():
        return _FakeResponse(choices=[_FakeChoice(
            finish_reason="tool_calls",
            message=_FakeMessage(content=None, tool_calls=[_FakeToolCall(
                id="tc_inf",
                function=_FakeFunction(
                    name="get_quote",
                    arguments=json.dumps({"symbol": "SPY"}),
                ),
            )]),
        )])

    # Provide max_iter+1 = 5 looping responses for max_iter=4 (respond limit).
    scripted = [_looping_response() for _ in range(20)]

    # Stub get_quote to avoid a live IBKR call.
    async def stub_get_quote(_args):
        return json.dumps({"symbol": "SPY", "last": 600.0})
    from pipelines import tool_dispatch
    monkeypatch.setitem(tool_dispatch.AGENT_TOOL_REGISTRY, "get_quote", stub_get_quote)

    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_llm_client(scripted),
    )

    result = await runner.run_skill(test_agent, "respond")
    assert result.finish_reason == "max_iter"
    assert "cap hit" in result.final_text


async def test_respond_works_for_mike_no_workspace_dir(monkeypatch):
    """mike has agents/mike.yaml but no agents/mike/ dir. Pipeline must still
    work — read_workspace returns empty, the rest of the bundle proceeds.
    Inserts a mike-tagged inbox row, runs respond, cleans up."""
    from db import store
    from db.schema import get_pool

    inbox_id = await store.post_to_inbox("mike", "what's the desk's net beta?")
    reply_text = "Net beta ~0.4 from current allocations."

    scripted = [
        _FakeResponse(choices=[_FakeChoice(
            finish_reason="tool_calls",
            message=_FakeMessage(content=None, tool_calls=[_FakeToolCall(
                id="tc_mike",
                function=_FakeFunction(
                    name="mark_inbox_responded",
                    arguments=json.dumps({
                        "inbox_id": inbox_id,
                        "response_body": reply_text,
                        "agent_name": "mike",
                    }),
                ),
            )]),
        )]),
        _FakeResponse(choices=[_FakeChoice(
            finish_reason="stop",
            message=_FakeMessage(content="done"),
        )]),
    ]
    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_llm_client(scripted, session_id=kw.get("session_id") or "mike-test"),
    )

    try:
        result = await runner.run_skill("mike", "respond")
        assert result.skipped is False
        assert result.finish_reason == "stop"
        recent = await store.get_recent_inbox("mike", limit=1)
        assert recent[0]["response_body"] == reply_text
    finally:
        # Cleanup the mike inbox row our test inserted.
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM agent_inbox WHERE id=$1", inbox_id)


async def test_unknown_tool_call_yields_error_to_llm(test_agent, monkeypatch):
    """LLM calls a tool not in the allowlist → dispatch returns {'error': ...}."""
    inbox_id = await store.post_to_inbox(test_agent, "Q")

    scripted = [
        _FakeResponse(choices=[_FakeChoice(
            finish_reason="tool_calls",
            message=_FakeMessage(content=None, tool_calls=[_FakeToolCall(
                id="tc_bad",
                function=_FakeFunction(
                    name="place_order",
                    arguments=json.dumps({"symbol": "SPY", "qty": 100}),
                ),
            )]),
        )]),
        _FakeResponse(choices=[_FakeChoice(
            finish_reason="tool_calls",
            message=_FakeMessage(content=None, tool_calls=[_FakeToolCall(
                id="tc_recover",
                function=_FakeFunction(
                    name="mark_inbox_responded",
                    arguments=json.dumps({
                        "inbox_id": inbox_id,
                        "response_body": "Sorry, can't place orders.",
                        "agent_name": test_agent,
                    }),
                ),
            )]),
        )]),
        _FakeResponse(choices=[_FakeChoice(
            finish_reason="stop",
            message=_FakeMessage(content="Done."),
        )]),
    ]

    monkeypatch.setattr(
        runner.llm_client, "make_client",
        lambda *a, **kw: _stub_llm_client(scripted),
    )

    result = await runner.run_skill(test_agent, "respond")
    # The first tool call should have logged an error result.
    bad_log = [c for c in result.tool_call_log if c["name"] == "place_order"]
    assert len(bad_log) == 1
    assert "error" in bad_log[0]["result"]
    # The LLM recovered and the inbox row flipped.
    assert (await store.get_pending_inbox(test_agent)) == []
