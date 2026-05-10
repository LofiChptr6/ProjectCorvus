"""Unit tests for pipeline components — no DB, no LLM."""
from __future__ import annotations

import json

import pytest

from pipelines import guardrails, tool_dispatch, tool_loop


# ── tool_dispatch ─────────────────────────────────────────────────────────────


async def test_dispatch_unknown_returns_error():
    raw = await tool_dispatch.dispatch("does_not_exist", {})
    payload = json.loads(raw)
    assert "error" in payload
    assert "unknown tool" in payload["error"]


async def test_dispatch_handler_exception_envelopes_error(monkeypatch):
    async def bad_handler(_args):
        raise RuntimeError("boom")

    monkeypatch.setitem(tool_dispatch.AGENT_TOOL_REGISTRY, "_test_explode", bad_handler)
    raw = await tool_dispatch.dispatch("_test_explode", {})
    payload = json.loads(raw)
    assert payload["error"].startswith("RuntimeError")


def test_filter_schemas_allowlist():
    schemas = tool_dispatch.filter_schemas({"get_quote", "compute_technicals"})
    names = {s["name"] for s in schemas}
    assert names == {"get_quote", "compute_technicals"}


def test_filter_schemas_none_returns_all():
    schemas = tool_dispatch.filter_schemas(None)
    assert len(schemas) == len(tool_dispatch.TOOL_SCHEMAS)


def test_filter_schemas_drops_unknown_names():
    schemas = tool_dispatch.filter_schemas({"get_quote", "not_a_tool"})
    names = {s["name"] for s in schemas}
    assert names == {"get_quote"}


# ── tool_loop ─────────────────────────────────────────────────────────────────


def test_to_openai_tools_translates_shape():
    anth = [{
        "name": "foo",
        "description": "does foo",
        "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
    }]
    out = tool_loop.to_openai_tools(anth)
    assert out == [{
        "type": "function",
        "function": {
            "name": "foo",
            "description": "does foo",
            "parameters": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        },
    }]


def test_to_openai_tools_empty_input():
    assert tool_loop.to_openai_tools([]) == []


def test_to_openai_tools_missing_input_schema_defaults_to_empty_object():
    out = tool_loop.to_openai_tools([{"name": "x", "description": "y"}])
    assert out[0]["function"]["parameters"] == {"type": "object", "properties": {}}


# ── guardrails ────────────────────────────────────────────────────────────────


def test_allowlist_for_respond_has_mark_inbox_responded():
    aw = guardrails.allowlist_for("respond")
    assert "mark_inbox_responded" in aw
    # Must NOT include trading writes — respond stays in its lane.
    assert "submit_conviction_view" not in aw
    assert "place_order" not in aw


def test_allowlist_for_unknown_raises():
    with pytest.raises(KeyError):
        guardrails.allowlist_for("not_a_real_skill_type")


def test_limits_for_respond_is_capped():
    lim = guardrails.limits_for("respond")
    assert lim["max_iter"] <= 6
    assert lim["max_tokens"] <= 4000


def test_limits_for_unknown_returns_safe_default():
    lim = guardrails.limits_for("brand_new_skill")
    assert "max_iter" in lim and "max_tokens" in lim
