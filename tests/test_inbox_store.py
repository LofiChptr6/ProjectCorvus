"""db/store.py inbox functions — round-trip + ownership + edge cases."""
from __future__ import annotations

import pytest

from db import store


async def test_post_get_pending_round_trip(test_agent):
    inbox_id = await store.post_to_inbox(test_agent, "what's your read on TLT?", sender="user")
    assert isinstance(inbox_id, int) and inbox_id > 0

    pending = await store.get_pending_inbox(test_agent)
    assert len(pending) == 1
    row = pending[0]
    assert row["id"] == inbox_id
    assert row["agent_name"] == test_agent
    assert row["body"] == "what's your read on TLT?"
    assert row["sender"] == "user"


async def test_get_pending_filters_by_agent(test_agent):
    other = test_agent + "_other"
    await store.post_to_inbox(test_agent, "Q1")
    await store.post_to_inbox(other, "Q2")

    pending = await store.get_pending_inbox(test_agent)
    bodies = [p["body"] for p in pending]
    assert "Q1" in bodies and "Q2" not in bodies

    # Cleanup the second agent's row (test_agent fixture only cleans test_agent).
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM agent_inbox WHERE agent_name=$1", other)


async def test_mark_responded_happy_path(test_agent):
    inbox_id = await store.post_to_inbox(test_agent, "Q")
    ok = await store.mark_inbox_responded(inbox_id, "A", test_agent, response_session_id="sess-123")
    assert ok is True

    pending = await store.get_pending_inbox(test_agent)
    assert pending == []  # row no longer pending

    recent = await store.get_recent_inbox(test_agent)
    assert len(recent) == 1
    assert recent[0]["response_body"] == "A"
    assert recent[0]["responded_at"] is not None


async def test_mark_responded_wrong_owner_rejected(test_agent):
    inbox_id = await store.post_to_inbox(test_agent, "Q")
    ok = await store.mark_inbox_responded(inbox_id, "A", "imposter")
    assert ok is False

    # Original row should still be pending.
    pending = await store.get_pending_inbox(test_agent)
    assert len(pending) == 1


async def test_mark_responded_already_responded_rejected(test_agent):
    inbox_id = await store.post_to_inbox(test_agent, "Q")
    assert await store.mark_inbox_responded(inbox_id, "A1", test_agent) is True
    # Second mark should fail because responded_at is no longer NULL.
    assert await store.mark_inbox_responded(inbox_id, "A2", test_agent) is False

    recent = await store.get_recent_inbox(test_agent)
    # Original answer must be preserved.
    assert recent[0]["response_body"] == "A1"


async def test_mark_responded_unknown_id_rejected(test_agent):
    ok = await store.mark_inbox_responded(99999999, "A", test_agent)
    assert ok is False


async def test_pending_ordered_oldest_first(test_agent):
    """get_pending_inbox returns FIFO so the LLM answers oldest first."""
    ids = []
    for i in range(3):
        ids.append(await store.post_to_inbox(test_agent, f"Q{i}"))

    pending = await store.get_pending_inbox(test_agent)
    assert [p["id"] for p in pending] == ids  # ascending by created_at


async def test_recent_inbox_includes_both_states(test_agent):
    pending_id = await store.post_to_inbox(test_agent, "Pending Q")
    responded_id = await store.post_to_inbox(test_agent, "Responded Q")
    await store.mark_inbox_responded(responded_id, "Reply", test_agent)

    recent = await store.get_recent_inbox(test_agent)
    statuses = {(r["id"], r["responded_at"] is not None) for r in recent}
    assert (pending_id, False) in statuses
    assert (responded_id, True) in statuses
