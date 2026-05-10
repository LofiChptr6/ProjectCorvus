"""obs/proxy.py — _persist_openai_exchange writes audit_log + tool_calls rows.

We test the persistence function directly (unit-level) rather than going
through the FastAPI HTTP layer; the relay code is thin passthrough and adds
nothing beyond what _persist_openai_exchange does for audit purposes.
"""
from __future__ import annotations

import json
import time
import uuid

import pytest


def _now_minus(ms: int) -> float:
    return time.time() - ms / 1000.0

from db.schema import get_pool
from obs import proxy


def _fake_openai_response(content: str = "ok", tool_calls=None, finish_reason="stop") -> dict:
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test",
        "model": "test-model",
        "choices": [{"finish_reason": finish_reason, "message": msg, "index": 0}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
    }


async def _audit_for_session(session_id: str) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM audit_log WHERE session_id=$1 ORDER BY id ASC",
            session_id,
        )
        return [dict(r) for r in rows]


async def _tool_calls_for_session(session_id: str) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM tool_calls WHERE session_id=$1 ORDER BY id ASC",
            session_id,
        )
        return [dict(r) for r in rows]


async def _cleanup_session(session_id: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM audit_log WHERE session_id=$1", session_id)
        await conn.execute("DELETE FROM tool_calls WHERE session_id=$1", session_id)


async def test_persist_openai_writes_audit_row():
    sid = f"test-openai-{uuid.uuid4().hex[:8]}"
    body = {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "You are atlas."},
            {"role": "user", "content": "Hello?"},
        ],
    }
    resp = _fake_openai_response(content="Hi.", finish_reason="stop")

    try:
        await proxy._persist_openai_exchange(
            session_id=sid, skill="atlas-review", agent="atlas",
            body_dict=body, resp_data=resp, started_at=_now_minus(50), error=None,
        )
        rows = await _audit_for_session(sid)
        assert len(rows) == 1
        r = rows[0]
        assert r["agent_name"] == "atlas"
        assert r["routine"] == "atlas-review"
        assert r["finish_reason"] == "stop"
        assert r["final_response"] == "Hi."
        assert r["prompt_tokens"] == 50
        assert r["completion_tokens"] == 30
        # System prompt should be lifted from messages[0].
        assert r["system_prompt"] == "You are atlas."
        # The full conversation should include the assistant turn.
        msgs = json.loads(r["messages"])
        assert msgs[-1]["role"] == "assistant"
        assert msgs[-1]["content"] == "Hi."
    finally:
        await _cleanup_session(sid)


async def test_persist_openai_writes_tool_call_rows():
    sid = f"test-openai-tc-{uuid.uuid4().hex[:8]}"
    body = {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Quote SPY?"},
        ],
    }
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_quote", "arguments": '{"symbol": "SPY"}'},
        },
        {
            "id": "call_2",
            "type": "function",
            "function": {"name": "compute_technicals", "arguments": '{"symbol": "SPY", "indicators": ["RSI_14"]}'},
        },
    ]
    resp = _fake_openai_response(content="", tool_calls=tool_calls, finish_reason="tool_calls")

    try:
        await proxy._persist_openai_exchange(
            session_id=sid, skill="atlas-review", agent="atlas",
            body_dict=body, resp_data=resp, started_at=_now_minus(50), error=None,
        )

        audit = await _audit_for_session(sid)
        assert len(audit) == 1
        assert audit[0]["tool_rounds"] == 2

        tcs = await _tool_calls_for_session(sid)
        assert len(tcs) == 2
        names = {r["tool_name"] for r in tcs}
        assert names == {"get_quote", "compute_technicals"}
        # tool_input should be parsed JSON, not a raw string.
        for r in tcs:
            assert isinstance(json.loads(r["tool_input"]), dict)
    finally:
        await _cleanup_session(sid)


async def test_persist_openai_handles_error_envelope():
    sid = f"test-openai-err-{uuid.uuid4().hex[:8]}"
    body = {"messages": [{"role": "user", "content": "x"}]}
    resp = {}  # vllm errored, no body

    try:
        await proxy._persist_openai_exchange(
            session_id=sid, skill="atlas-review", agent="atlas",
            body_dict=body, resp_data=resp, started_at=_now_minus(50),
            error="vllm 502: upstream gone",
        )

        rows = await _audit_for_session(sid)
        assert len(rows) == 1
        r = rows[0]
        assert r["error"] == "vllm 502: upstream gone"
        # Error path → finish_reason should fall through to 'error'.
        assert r["finish_reason"] == "error"
        assert r["final_response"] == ""
    finally:
        await _cleanup_session(sid)


async def test_persist_openai_request_index_increments_per_session():
    sid = f"test-openai-idx-{uuid.uuid4().hex[:8]}"
    body1 = {"messages": [{"role": "user", "content": "first"}]}
    body2 = {"messages": [{"role": "user", "content": "second"}]}
    r1 = _fake_openai_response(content="r1")
    r2 = _fake_openai_response(content="r2")

    try:
        await proxy._persist_openai_exchange(
            session_id=sid, skill="atlas-review", agent="atlas",
            body_dict=body1, resp_data=r1, started_at=_now_minus(50), error=None,
        )
        await proxy._persist_openai_exchange(
            session_id=sid, skill="atlas-review", agent="atlas",
            body_dict=body2, resp_data=r2, started_at=_now_minus(40), error=None,
        )

        rows = await _audit_for_session(sid)
        assert len(rows) == 2
        # request_index column should advance.
        assert rows[0]["request_index"] != rows[1]["request_index"]
    finally:
        await _cleanup_session(sid)
