"""Verify the per-conviction custom-expiration contract:
- `expires_in_hours` is REQUIRED at every layer (DB, MCP tool, schema)
- Bounds: 5 min (≈0.0833h) ≤ x ≤ 720h (30 days)
- Out-of-range values raise; the conviction is NOT written

Layers covered:
- db.store.upsert_conviction (live write path)
- db.store.insert_conviction_shadow (dry-run write path)
- pipelines.schemas.ConvictionView (LLM-output validation)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ── db.store.upsert_conviction ────────────────────────────────────────────────


async def test_upsert_conviction_rejects_under_5min(test_agent):
    import db.store as store
    with pytest.raises(ValueError, match="below minimum"):
        await store.upsert_conviction(
            agent_name=test_agent, symbol="SPY",
            direction="long", conviction=0.5,
            expires_in_hours=0.05,  # 3 minutes — below 5 min floor
            expected_return_pct=1.0, time_to_target_days=1,
        )


async def test_upsert_conviction_rejects_over_30days(test_agent):
    import db.store as store
    with pytest.raises(ValueError, match="above maximum"):
        await store.upsert_conviction(
            agent_name=test_agent, symbol="SPY",
            direction="long", conviction=0.5,
            expires_in_hours=721,  # 30 days + 1 hour — above 720 ceiling
            expected_return_pct=1.0, time_to_target_days=1,
        )


async def test_upsert_conviction_accepts_exact_5min(test_agent):
    import db.store as store
    view_id = await store.upsert_conviction(
        agent_name=test_agent, symbol="SPY",
        direction="long", conviction=0.5,
        expires_in_hours=5/60,  # exactly 5 min
        expected_return_pct=1.0, time_to_target_days=1,
    )
    assert view_id > 0


async def test_upsert_conviction_accepts_exact_30days(test_agent):
    import db.store as store
    view_id = await store.upsert_conviction(
        agent_name=test_agent, symbol="QQQ",
        direction="long", conviction=0.5,
        expires_in_hours=720,  # exactly 30 days
        expected_return_pct=1.0, time_to_target_days=30,
    )
    assert view_id > 0


async def test_upsert_conviction_requires_expires_in_hours(test_agent):
    """No default — call without `expires_in_hours` must TypeError."""
    import db.store as store
    with pytest.raises(TypeError):
        await store.upsert_conviction(
            agent_name=test_agent, symbol="SPY",
            direction="long", conviction=0.5,
            expected_return_pct=1.0, time_to_target_days=1,
        )


async def test_upsert_conviction_sets_expires_at_correctly(test_agent):
    """Fractional hours land at the correct expires_at timestamp."""
    import db.store as store
    from db.schema import get_pool
    before = datetime.now(timezone.utc)
    await store.upsert_conviction(
        agent_name=test_agent, symbol="IWM",
        direction="long", conviction=0.3,
        expires_in_hours=2.5,  # 2h30m
        expected_return_pct=1.0, time_to_target_days=1,
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT expires_at FROM agent_conviction WHERE agent_name=$1 AND symbol='IWM'",
            test_agent,
        )
    delta_s = (row["expires_at"] - before).total_seconds()
    # 2.5h = 9000s; allow ±5s for query latency
    assert 8995 <= delta_s <= 9020, f"expected ~9000s, got {delta_s}"


# ── pipelines.schemas.ConvictionView ──────────────────────────────────────────


def test_conviction_view_schema_requires_expires_in_hours():
    from pipelines import schemas
    import pydantic
    with pytest.raises(pydantic.ValidationError, match="expires_in_hours"):
        schemas.ConvictionView(
            symbol="SPY", direction="long", conviction=0.5,
            expected_return_pct=1.0, time_to_target_days=1,
        )


def test_conviction_view_schema_rejects_zero():
    from pipelines import schemas
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        schemas.ConvictionView(
            symbol="SPY", direction="long", conviction=0.5,
            expected_return_pct=1.0, time_to_target_days=1,
            expires_in_hours=0,
        )


def test_conviction_view_schema_rejects_over_30days():
    from pipelines import schemas
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        schemas.ConvictionView(
            symbol="SPY", direction="long", conviction=0.5,
            expected_return_pct=1.0, time_to_target_days=1,
            expires_in_hours=721,
        )


def test_conviction_view_schema_accepts_float_5min():
    from pipelines import schemas
    cv = schemas.ConvictionView(
        symbol="SPY", direction="long", conviction=0.5,
        expected_return_pct=1.0, time_to_target_days=1,
        expires_in_hours=5/60,
    )
    assert cv.expires_in_hours == pytest.approx(5/60)


# ── shadow-table write ────────────────────────────────────────────────────────


async def test_insert_conviction_shadow_requires_expires_in_hours(test_agent):
    import db.store as store
    with pytest.raises(TypeError):
        await store.insert_conviction_shadow(
            agent_name=test_agent, symbol="SPY",
            direction="long", conviction=0.5,
        )
