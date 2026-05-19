"""Unit tests for the Phase-2 time-fallback in `_get_fill_context`.

When the agent_ledger join comes up empty, the resolver searches for the
nearest allocation_decision within ±90s and synthesizes a contributors list
from `contributing_views_json[symbol]` so the panel can still surface useful
attribution.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import obs.queries as Q


def _fake_conn(fetchrow_seq, fetch_seq):
    """Build a MagicMock that emulates asyncpg.Connection's fetchrow/fetch
    by popping pre-canned values off `fetchrow_seq` and `fetch_seq`. Each
    test wires the sequences to match the exact query order it expects
    `_get_fill_context` to issue."""
    conn = MagicMock()
    fr = list(fetchrow_seq)
    f = list(fetch_seq)

    async def fetchrow(*args, **kwargs):
        return fr.pop(0) if fr else None

    async def fetch(*args, **kwargs):
        return f.pop(0) if f else []

    conn.fetchrow = fetchrow
    conn.fetch = fetch
    return conn


def test_time_fallback_synthesizes_contributors_from_nearby_decision():
    fill_dt = datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc)
    fill_row = {
        "id": 271, "symbol": "DLR",
        "filled_at": fill_dt.isoformat(),
        "action": "BOT", "quantity": 33, "fill_price": 192.23,
        "order_id": 999, "agent_name": "mike",
    }
    # No ledger rows → triggers the fallback
    ledger_rows: list[dict] = []
    # No decision_d via the original ledger.decision_id path
    decision_row = None
    # Nearby allocation_decision matching DLR with two contributors
    nearby_decision = {
        "id": 412, "decided_at": fill_dt - timedelta(seconds=30),
        "nav_at_decision": 1_000_000.0, "notes": "dry_run; cash_weight=0.05",
        "contributing_views_json": json.dumps({
            "DLR": [
                {"agent": "atlas", "weight": 0.35},
                {"agent": "commodity", "weight": 0.22},
            ],
        }),
    }

    # Call sequence under empty-ledger:
    #   fetchrow → fill_row
    #   fetch    → ledger_rows ([])
    #   (decision_id None → skip decision fetch + skip per-contributor block)
    #   fetchrow → nearby_decision
    async def fake_with_conn(fn):
        conn = _fake_conn(
            fetchrow_seq=[fill_row, nearby_decision],
            fetch_seq=[ledger_rows],
        )
        return await fn(conn)

    with patch.object(Q, "_with_conn", side_effect=fake_with_conn):
        ctx = asyncio.run(Q._get_fill_context("DLR", fill_dt.isoformat()))

    assert ctx is not None
    assert ctx["fill"]["id"] == 271
    assert ctx["attribution_source"] == "inferred_by_time"
    agents = {c["agent_name"] for c in ctx["contributors"]}
    assert agents == {"atlas", "commodity"}
    # No ledger event since these are inferred
    assert all(c["ledger_event"] is None for c in ctx["contributors"])
    # inferred_weight populated from contributing_views_json
    weights = {c["agent_name"]: c["inferred_weight"] for c in ctx["contributors"]}
    assert abs(weights["atlas"] - 0.35) < 1e-9
    assert abs(weights["commodity"] - 0.22) < 1e-9
    # Decision surfaced from the fallback for the panel header
    assert ctx["decision"]["id"] == 412


def test_attribution_source_stays_ledger_when_rows_exist():
    """When the ledger join returns rows, attribution_source is 'ledger'
    and the time-fallback never fires."""
    fill_dt = datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc)
    fill_row = {
        "id": 100, "symbol": "DLR",
        "filled_at": fill_dt.isoformat(),
        "action": "BOT", "quantity": 10, "fill_price": 200.0,
        "order_id": 555, "agent_name": "mike",
    }
    ledger_rows = [{
        "agent_name": "atlas", "event": "LEND", "qty": 10, "price_per_share": 200.0,
        "realized_pnl": None, "decision_id": 50, "booked_at": fill_dt,
    }]
    decision_row = {
        "id": 50, "decided_at": fill_dt, "nav_at_decision": 1_000_000.0, "notes": "live",
    }
    # Call sequence under non-empty-ledger:
    #   fetchrow → fill_row
    #   fetch    → ledger_rows (1 row)
    #   fetchrow → decision_row (allocation_decision lookup, decision_id=50)
    #   per ledger row:
    #     fetchrow → conviction (None for this test)
    #     fetch    → theses ([])
    #     fetchrow → session (None — falls through to heuristic)
    #     fetchrow → session via heuristic (None)
    #   (contributors is non-empty → fallback skipped)
    async def fake_with_conn(fn):
        conn = _fake_conn(
            fetchrow_seq=[fill_row, decision_row, None, None, None],
            fetch_seq=[ledger_rows, []],
        )
        return await fn(conn)

    with patch.object(Q, "_with_conn", side_effect=fake_with_conn):
        ctx = asyncio.run(Q._get_fill_context("DLR", fill_dt.isoformat()))

    assert ctx["attribution_source"] == "ledger"
    assert len(ctx["contributors"]) == 1
    assert ctx["contributors"][0]["agent_name"] == "atlas"
    assert ctx["contributors"][0]["ledger_event"] == "LEND"


def test_no_contributors_no_nearby_decision_returns_clean_empty_state():
    """Truly orphan: no ledger, no nearby decision. ctx returned with
    empty contributors and `attribution_source='ledger'` (unchanged default)."""
    fill_dt = datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc)
    fill_row = {
        "id": 1, "symbol": "ZZZ",
        "filled_at": fill_dt.isoformat(),
        "action": "BOT", "quantity": 1, "fill_price": 1.0,
        "order_id": 1, "agent_name": "mike",
    }

    # Call sequence under empty-ledger + no-nearby-decision:
    #   fetchrow → fill_row
    #   fetch    → ledger_rows ([])
    #   fetchrow → None (no nearby allocation_decision)
    async def fake_with_conn(fn):
        conn = _fake_conn(
            fetchrow_seq=[fill_row, None],
            fetch_seq=[[]],
        )
        return await fn(conn)

    with patch.object(Q, "_with_conn", side_effect=fake_with_conn):
        ctx = asyncio.run(Q._get_fill_context("ZZZ", fill_dt.isoformat()))

    assert ctx["contributors"] == []
    assert ctx["attribution_source"] == "ledger"  # never escalated
