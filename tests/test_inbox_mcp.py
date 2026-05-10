"""MCP tool wrappers around the inbox store. Verifies the JSON envelope shape
agents receive when calling /get_my_inbox and /mark_inbox_responded.

FastMCP wraps tool functions in FunctionTool — to invoke the underlying coroutine
we walk through the registered tool object's `.fn` attribute (or call directly via
the .run method depending on FastMCP version)."""
from __future__ import annotations

import json

import pytest

from db import store


async def _call_tool(name: str, **kwargs):
    """Invoke a registered FastMCP tool by name and return the parsed JSON."""
    import mcp_server
    tool = await mcp_server.mcp.get_tool(name)
    # FastMCP 3.x: tool exposes `.fn` (the original async function).
    fn = getattr(tool, "fn", None)
    if fn is None:
        # Fallback for other versions: tool itself is callable.
        fn = tool
    raw = await fn(**kwargs)
    return json.loads(raw)


async def test_get_my_inbox_returns_pending(test_agent):
    inbox_id = await store.post_to_inbox(test_agent, "Q")
    payload = await _call_tool("get_my_inbox", agent_name=test_agent)
    assert "pending" in payload
    assert len(payload["pending"]) == 1
    assert payload["pending"][0]["id"] == inbox_id
    assert payload["pending"][0]["body"] == "Q"


async def test_get_my_inbox_empty_for_unknown_agent(test_agent):
    payload = await _call_tool("get_my_inbox", agent_name=test_agent)
    assert payload == {"pending": []}


async def test_mark_inbox_responded_happy_path(test_agent):
    inbox_id = await store.post_to_inbox(test_agent, "Q")
    payload = await _call_tool(
        "mark_inbox_responded",
        inbox_id=inbox_id,
        response_body="A",
        agent_name=test_agent,
    )
    assert payload == {"updated": True}

    # Confirm the underlying row is now responded.
    pending = await store.get_pending_inbox(test_agent)
    assert pending == []


async def test_mark_inbox_responded_wrong_owner(test_agent):
    inbox_id = await store.post_to_inbox(test_agent, "Q")
    payload = await _call_tool(
        "mark_inbox_responded",
        inbox_id=inbox_id,
        response_body="A",
        agent_name="imposter",
    )
    assert payload == {"updated": False}
