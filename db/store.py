"""Async database access layer (PostgreSQL / asyncpg). All writes go through here."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Optional

from db.schema import DB_PATH, get_pool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Audit log ────────────────────────────────────────────────────────────────

async def write_audit_log(
    session_id: str,
    agent_name: str,
    routine: str,
    trigger_source: str,
    system_prompt: str,
    messages: list[dict],
    tool_rounds: int,
    final_response: Optional[str],
    finish_reason: str,
    duration_ms: int,
    prompt_tokens: int,
    completion_tokens: int,
    error: Optional[str] = None,
    db_path: str = DB_PATH,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO audit_log
               (session_id, created_at, agent_name, routine, trigger_source,
                system_prompt, messages, tool_rounds, final_response,
                finish_reason, duration_ms, prompt_tokens, completion_tokens, error)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)""",
            session_id, _now(), agent_name, routine, trigger_source,
            system_prompt, json.dumps(messages), tool_rounds,
            final_response, finish_reason, duration_ms,
            prompt_tokens, completion_tokens, error,
        )


async def get_audit_log(session_id: str, db_path: str = DB_PATH) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM audit_log WHERE session_id=$1", session_id)
        return dict(row) if row else None


async def list_audit_logs(
    agent_name: Optional[str] = None,
    limit: int = 20,
    db_path: str = DB_PATH,
) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if agent_name:
            rows = await conn.fetch(
                "SELECT * FROM audit_log WHERE agent_name=$1 ORDER BY id DESC LIMIT $2",
                agent_name, limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT $1", limit,
            )
        return [dict(r) for r in rows]


# ── Tool calls ───────────────────────────────────────────────────────────────

async def write_tool_call(
    session_id: str,
    tool_round: int,
    tool_name: str,
    tool_input: dict,
    tool_output: Optional[str],
    duration_ms: int,
    error: Optional[str] = None,
    db_path: str = DB_PATH,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO tool_calls
               (session_id, created_at, tool_round, tool_name, tool_input, tool_output, duration_ms, error)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
            session_id, _now(), tool_round, tool_name,
            json.dumps(tool_input), tool_output, duration_ms, error,
        )


# ── Orders ───────────────────────────────────────────────────────────────────

async def write_order(
    session_id: Optional[str],
    agent_name: str,
    symbol: str,
    action: str,
    order_type: str,
    quantity: float,
    limit_price: Optional[float],
    stop_price: Optional[float],
    status: str,
    risk_approved: bool,
    human_approved: Optional[bool],
    rejection_reason: Optional[str],
    reasoning: Optional[str],
    mode: str,
    ibkr_order_id: Optional[int] = None,
    db_path: str = DB_PATH,
) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO orders
               (session_id, agent_name, created_at, ibkr_order_id, symbol, action,
                order_type, quantity, limit_price, stop_price, status,
                risk_approved, human_approved, rejection_reason, reasoning, mode)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
               RETURNING id""",
            session_id, agent_name, _now(), ibkr_order_id, symbol, action,
            order_type, quantity, limit_price, stop_price, status,
            int(risk_approved),
            None if human_approved is None else int(human_approved),
            rejection_reason, reasoning, mode,
        )
        return int(row["id"])


async def update_order_status(
    order_id: int,
    status: str,
    ibkr_order_id: Optional[int] = None,
    db_path: str = DB_PATH,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if ibkr_order_id is not None:
            await conn.execute(
                "UPDATE orders SET status=$1, ibkr_order_id=$2 WHERE id=$3",
                status, ibkr_order_id, order_id,
            )
        else:
            await conn.execute(
                "UPDATE orders SET status=$1 WHERE id=$2", status, order_id,
            )


# ── Fills ────────────────────────────────────────────────────────────────────

async def write_fill(
    ibkr_exec_id: str,
    order_id: Optional[int],
    agent_name: Optional[str],
    filled_at: str,
    symbol: str,
    action: str,
    quantity: float,
    fill_price: float,
    commission: Optional[float],
    exchange: Optional[str],
    mode: str,
    db_path: str = DB_PATH,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO fills
               (ibkr_exec_id, order_id, agent_name, filled_at, symbol, action,
                quantity, fill_price, commission, exchange, mode)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
               ON CONFLICT (ibkr_exec_id) DO NOTHING""",
            ibkr_exec_id, order_id, agent_name, filled_at,
            symbol, action, quantity, fill_price, commission, exchange, mode,
        )


async def get_fills(
    symbol: Optional[str] = None,
    date: Optional[str] = None,
    agent_name: Optional[str] = None,
    limit: int = 50,
    db_path: str = DB_PATH,
) -> list[dict]:
    conditions: list[str] = []
    params: list[Any] = []
    i = 1
    if symbol:
        conditions.append(f"symbol=${i}")
        params.append(symbol)
        i += 1
    if date:
        # filled_at is stored as ISO string 'YYYY-MM-DDTHH:MM:SS...'; match the date prefix.
        conditions.append(f"LEFT(filled_at, 10)=${i}")
        params.append(date)
        i += 1
    if agent_name:
        conditions.append(f"agent_name=${i}")
        params.append(agent_name)
        i += 1
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    sql = f"SELECT * FROM fills {where} ORDER BY filled_at DESC LIMIT ${i}"
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


# ── Kill switch ───────────────────────────────────────────────────────────────

async def is_killed(agent_name: Optional[str] = None, db_path: str = DB_PATH) -> bool:
    """Return True if the global kill switch OR agent-specific kill switch is active."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_active FROM kill_switch WHERE agent_name IS NULL ORDER BY id DESC LIMIT 1"
        )
        if row and row["is_active"]:
            return True
        if agent_name:
            row = await conn.fetchrow(
                "SELECT is_active FROM kill_switch WHERE agent_name=$1 ORDER BY id DESC LIMIT 1",
                agent_name,
            )
            if row and row["is_active"]:
                return True
    return False


async def set_kill_switch(
    active: bool,
    agent_name: Optional[str] = None,
    activated_by: str = "cli",
    reason: Optional[str] = None,
    db_path: str = DB_PATH,
) -> None:
    now = _now()
    pool = await get_pool()
    async with pool.acquire() as conn:
        if active:
            await conn.execute(
                """INSERT INTO kill_switch (agent_name, is_active, activated_at, activated_by, reason)
                   VALUES ($1, 1, $2, $3, $4)""",
                agent_name, now, activated_by, reason,
            )
        else:
            # "IS NOT DISTINCT FROM" handles NULL equality for the global row.
            await conn.execute(
                """UPDATE kill_switch SET is_active=0, deactivated_at=$1
                   WHERE agent_name IS NOT DISTINCT FROM $2 AND is_active=1""",
                now, agent_name,
            )


# ── Agent allocations ─────────────────────────────────────────────────────────

async def set_allocation(
    agent_name: str, allocation_pct: float, updated_by: str = "cli", db_path: str = DB_PATH
) -> None:
    """Persist agent's NAV percentage (0.0–1.0). Dollar allocation is always derived as pct × live NAV."""
    if not 0.0 <= allocation_pct <= 1.0:
        raise ValueError(f"allocation_pct must be 0.0–1.0, got {allocation_pct}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_allocations (agent_name, allocation_pct, updated_at, updated_by)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (agent_name) DO UPDATE SET
                 allocation_pct=EXCLUDED.allocation_pct,
                 updated_at=EXCLUDED.updated_at,
                 updated_by=EXCLUDED.updated_by""",
            agent_name, allocation_pct, _now(), updated_by,
        )


async def get_allocations(db_path: str = DB_PATH) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM agent_allocations ORDER BY agent_name")
        return [dict(r) for r in rows]


# ── P&L ──────────────────────────────────────────────────────────────────────

async def upsert_pnl_daily(
    trade_date: str,
    agent_name: str,
    realized_pnl: float,
    unrealized_pnl: float,
    num_fills: int,
    nav_end: Optional[float] = None,
    db_path: str = DB_PATH,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO pnl_daily
               (trade_date, agent_name, realized_pnl, unrealized_pnl, total_pnl,
                nav_end, num_fills, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
               ON CONFLICT (trade_date, agent_name) DO UPDATE SET
                 realized_pnl=EXCLUDED.realized_pnl,
                 unrealized_pnl=EXCLUDED.unrealized_pnl,
                 total_pnl=EXCLUDED.realized_pnl+EXCLUDED.unrealized_pnl,
                 nav_end=EXCLUDED.nav_end,
                 num_fills=EXCLUDED.num_fills,
                 updated_at=EXCLUDED.updated_at""",
            trade_date, agent_name, realized_pnl, unrealized_pnl,
            realized_pnl + unrealized_pnl, nav_end, num_fills, _now(),
        )


async def get_pnl_summary(
    agent_name: Optional[str] = None,
    period: str = "today",
    db_path: str = DB_PATH,
) -> list[dict]:
    # Aggregates from agent_pnl_attribution (the live source of truth post-
    # 2026-04-26 sector migration). pnl_daily was the pre-migration daily roll-up
    # and is no longer written; reading it returned empty results.
    from datetime import date, timedelta
    today = date.today()
    params: list[Any] = []
    i = 1
    if period == "today":
        date_clause = f"decided_at::date = ${i}"
        params.append(today)
        i += 1
    elif period == "week":
        date_clause = f"decided_at::date >= ${i}"
        params.append(today - timedelta(days=7))
        i += 1
    elif period == "month":
        date_clause = f"decided_at::date >= ${i}"
        params.append(today - timedelta(days=30))
        i += 1
    else:
        date_clause = "TRUE"

    if agent_name:
        agent_clause = f"AND agent_name = ${i}"
        params.append(agent_name)
    else:
        agent_clause = ""

    sql = f"""
        SELECT agent_name,
               (decided_at::date)::text AS trade_date,
               COALESCE(SUM(attributed_pnl), 0)::float8 AS total_pnl,
               0.0::float8 AS realized_pnl,
               0.0::float8 AS unrealized_pnl,
               COUNT(*)::int AS num_fills
        FROM agent_pnl_attribution
        WHERE {date_clause} {agent_clause}
        GROUP BY agent_name, decided_at::date
        ORDER BY decided_at::date DESC, agent_name
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


# ── News ──────────────────────────────────────────────────────────────────────

async def write_news(
    symbol: Optional[str],
    headline: str,
    article_id: Optional[str],
    provider: Optional[str],
    db_path: str = DB_PATH,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO news_items (fetched_at, symbol, headline, article_id, provider) VALUES ($1,$2,$3,$4,$5)",
            _now(), symbol, headline, article_id, provider,
        )


async def get_recent_news(
    symbol: Optional[str] = None, limit: int = 10, db_path: str = DB_PATH
) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if symbol:
            rows = await conn.fetch(
                "SELECT * FROM news_items WHERE symbol=$1 ORDER BY fetched_at DESC LIMIT $2",
                symbol, limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM news_items ORDER BY fetched_at DESC LIMIT $1", limit,
            )
        return [dict(r) for r in rows]


# ── Agent thesis journal ─────────────────────────────────────────────────────

_VALID_THESIS_KINDS = {"hypothesis", "prediction", "observation", "question"}
_VALID_THESIS_STATUSES = {"open", "confirmed", "wrong", "superseded"}


async def record_thesis(
    agent_name: str,
    kind: str,
    title: str,
    body: str,
    verify_by: Optional[str] = None,
    parent_id: Optional[int] = None,
    market_snapshot: Optional[dict] = None,
) -> int:
    if kind not in _VALID_THESIS_KINDS:
        raise ValueError(f"kind must be one of {_VALID_THESIS_KINDS}, got {kind!r}")
    verify_by_dt = date.fromisoformat(verify_by) if isinstance(verify_by, str) else verify_by
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO agent_thesis
               (agent_name, kind, title, body, verify_by, parent_id, market_snapshot)
               VALUES ($1,$2,$3,$4,$5::date,$6,$7::jsonb)
               RETURNING id""",
            agent_name, kind, title, body, verify_by_dt, parent_id,
            json.dumps(market_snapshot) if market_snapshot is not None else None,
        )
        return int(row["id"])


async def update_thesis_status(
    thesis_id: int,
    status: str,
    resolution_note: Optional[str],
    agent_name: Optional[str] = None,
) -> bool:
    """Update status of a thesis. If `agent_name` is provided, ownership is enforced.
    Returns True if a row was updated."""
    if status not in _VALID_THESIS_STATUSES:
        raise ValueError(f"status must be one of {_VALID_THESIS_STATUSES}, got {status!r}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        if agent_name is not None:
            result = await conn.execute(
                """UPDATE agent_thesis
                   SET status=$1, resolution_note=$2,
                       resolved_at = CASE WHEN $1 IN ('confirmed','wrong','superseded')
                                          THEN NOW() ELSE resolved_at END
                   WHERE id=$3 AND agent_name=$4""",
                status, resolution_note, thesis_id, agent_name,
            )
        else:
            result = await conn.execute(
                """UPDATE agent_thesis
                   SET status=$1, resolution_note=$2,
                       resolved_at = CASE WHEN $1 IN ('confirmed','wrong','superseded')
                                          THEN NOW() ELSE resolved_at END
                   WHERE id=$3""",
                status, resolution_note, thesis_id,
            )
        # asyncpg returns "UPDATE n" — parse the count.
        return result.endswith(" 1")


async def get_open_theses(agent_name: str, limit: int = 10) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, created_at, kind, title, body, verify_by, parent_id, market_snapshot
               FROM agent_thesis
               WHERE agent_name=$1 AND status='open'
               ORDER BY created_at DESC
               LIMIT $2""",
            agent_name, limit,
        )
        return [dict(r) for r in rows]


async def get_theses_due(agent_name: str, on_or_before: str) -> list[dict]:
    on_or_before_dt = date.fromisoformat(on_or_before) if isinstance(on_or_before, str) else on_or_before
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, created_at, kind, title, body, verify_by
               FROM agent_thesis
               WHERE agent_name=$1 AND status='open'
                 AND verify_by IS NOT NULL AND verify_by <= $2::date
               ORDER BY verify_by ASC""",
            agent_name, on_or_before_dt,
        )
        return [dict(r) for r in rows]


async def get_recent_resolutions(agent_name: str, limit: int = 3) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, kind, title, status, resolution_note, resolved_at
               FROM agent_thesis
               WHERE agent_name=$1 AND status IN ('confirmed','wrong','superseded')
               ORDER BY resolved_at DESC NULLS LAST
               LIMIT $2""",
            agent_name, limit,
        )
        return [dict(r) for r in rows]


async def get_all_open_theses() -> dict[str, list[dict]]:
    """Mike-only consumer: return all agents' open theses, grouped by agent_name."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT agent_name, id, created_at, kind, title, body, verify_by
               FROM agent_thesis
               WHERE status='open'
               ORDER BY agent_name, created_at DESC"""
        )
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["agent_name"], []).append(dict(r))
        return out


# ── Agent tool gaps ──────────────────────────────────────────────────────────

_VALID_GAP_PRIORITIES = {"low", "normal", "high"}
_VALID_GAP_STATUSES = {"open", "acknowledged", "forwarded", "implemented", "declined"}


async def record_tool_gap(
    agent_name: str,
    tool_name: str,
    description: str,
    use_case: str,
    priority: str = "normal",
) -> int:
    if priority not in _VALID_GAP_PRIORITIES:
        raise ValueError(f"priority must be one of {_VALID_GAP_PRIORITIES}, got {priority!r}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO agent_tool_gaps
               (agent_name, tool_name, description, use_case, priority)
               VALUES ($1,$2,$3,$4,$5)
               RETURNING id""",
            agent_name, tool_name, description, use_case, priority,
        )
        return int(row["id"])


async def list_open_tool_gaps() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, agent_name, created_at, tool_name, description, use_case, priority
               FROM agent_tool_gaps
               WHERE status='open'
               ORDER BY created_at DESC"""
        )
        return [dict(r) for r in rows]


async def update_tool_gap_status(
    gap_id: int, status: str, mike_note: Optional[str] = None
) -> bool:
    if status not in _VALID_GAP_STATUSES:
        raise ValueError(f"status must be one of {_VALID_GAP_STATUSES}, got {status!r}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE agent_tool_gaps
               SET status=$1, mike_note=$2,
                   resolved_at = CASE WHEN $1 IN ('implemented','declined')
                                      THEN NOW() ELSE resolved_at END
               WHERE id=$3""",
            status, mike_note, gap_id,
        )
        return result.endswith(" 1")


# ── Agent evening digests ────────────────────────────────────────────────────

async def record_evening_digest(
    agent_name: str,
    trading_date: str,
    thesis_summary: Optional[str] = None,
    open_questions: Optional[str] = None,
    tomorrow_focus: Optional[str] = None,
    pnl_today: Optional[float] = None,
    pnl_week: Optional[float] = None,
    positions: Optional[list] = None,
    chart_path: Optional[str] = None,
) -> int:
    trading_date_dt = date.fromisoformat(trading_date) if isinstance(trading_date, str) else trading_date
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO agent_evening_digests
               (agent_name, trading_date, thesis_summary, open_questions, tomorrow_focus,
                pnl_today, pnl_week, positions_json, chart_path)
               VALUES ($1,$2::date,$3,$4,$5,$6,$7,$8::jsonb,$9)
               ON CONFLICT (agent_name, trading_date) DO UPDATE SET
                 thesis_summary = EXCLUDED.thesis_summary,
                 open_questions = EXCLUDED.open_questions,
                 tomorrow_focus = EXCLUDED.tomorrow_focus,
                 pnl_today      = EXCLUDED.pnl_today,
                 pnl_week       = EXCLUDED.pnl_week,
                 positions_json = EXCLUDED.positions_json,
                 chart_path     = EXCLUDED.chart_path
               RETURNING id""",
            agent_name, trading_date_dt, thesis_summary, open_questions, tomorrow_focus,
            pnl_today, pnl_week,
            json.dumps(positions) if positions is not None else None,
            chart_path,
        )
        return int(row["id"])


# ── Desk threads board ───────────────────────────────────────────────────────

_VALID_AUTHOR_KINDS = {"user", "agent", "external_feed", "system"}
_POST_BODY_MAX = 8000


async def create_thread(
    slug: str,
    title: str,
    description: Optional[str] = None,
    tags: Optional[list[str]] = None,
) -> int:
    if not slug or not title:
        raise ValueError("slug and title are required")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO thread (slug, title, description, tags)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (slug) DO UPDATE SET title=EXCLUDED.title,
                                                description=EXCLUDED.description,
                                                tags=EXCLUDED.tags
               RETURNING id""",
            slug, title, description, list(tags or []),
        )
        return int(row["id"])


async def list_threads(include_archived: bool = False) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT t.id, t.slug, t.title, t.description, t.tags,
                      t.created_at, t.archived_at,
                      (SELECT COUNT(*) FROM post p WHERE p.thread_id = t.id) AS post_count,
                      (SELECT MAX(posted_at) FROM post p WHERE p.thread_id = t.id) AS last_post_at
               FROM thread t
               WHERE ($1::boolean OR t.archived_at IS NULL)
               ORDER BY COALESCE(
                   (SELECT MAX(posted_at) FROM post p WHERE p.thread_id = t.id),
                   t.created_at
               ) DESC""",
            include_archived,
        )
        return [dict(r) for r in rows]


async def archive_thread(slug: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE thread SET archived_at = NOW() WHERE slug = $1 AND archived_at IS NULL",
            slug,
        )
        return result.endswith(" 1")


async def unarchive_thread(slug: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE thread SET archived_at = NULL WHERE slug = $1",
            slug,
        )
        return result.endswith(" 1")


async def post_to_thread(
    thread_slug: str,
    author: str,
    author_kind: str,
    body: str,
    title: Optional[str] = None,
    meta: Optional[dict] = None,
    parent_post_id: Optional[int] = None,
    expires_in_hours: Optional[float] = None,
) -> int:
    if author_kind not in _VALID_AUTHOR_KINDS:
        raise ValueError(f"author_kind must be one of {_VALID_AUTHOR_KINDS}, got {author_kind!r}")
    if not body or not body.strip():
        raise ValueError("post body must not be empty")
    if len(body) > _POST_BODY_MAX:
        raise ValueError(f"post body too long ({len(body)} > {_POST_BODY_MAX} chars)")
    pool = await get_pool()
    async with pool.acquire() as conn:
        thread_row = await conn.fetchrow("SELECT id FROM thread WHERE slug=$1", thread_slug)
        if not thread_row:
            raise ValueError(f"thread '{thread_slug}' not found — call create_thread first")
        thread_id = thread_row["id"]
        # Postgres doesn't allow NOW() + interval(variable hours) in default; build
        # the timestamp as a SQL expression.
        if expires_in_hours is not None and float(expires_in_hours) > 0:
            row = await conn.fetchrow(
                """INSERT INTO post (thread_id, author, author_kind, title, body, meta,
                                     parent_post_id, expires_at)
                   VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,
                           NOW() + ($8 || ' hours')::interval)
                   RETURNING id""",
                thread_id, author, author_kind, title, body,
                json.dumps(meta or {}),
                parent_post_id, str(float(expires_in_hours)),
            )
        else:
            row = await conn.fetchrow(
                """INSERT INTO post (thread_id, author, author_kind, title, body, meta,
                                     parent_post_id)
                   VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7)
                   RETURNING id""",
                thread_id, author, author_kind, title, body,
                json.dumps(meta or {}),
                parent_post_id,
            )
        return int(row["id"])


async def get_posts(
    thread_slug: str,
    limit: int = 20,
    before_id: Optional[int] = None,
    since_id: Optional[int] = None,
    author: Optional[str] = None,
    only_active: bool = True,
) -> list[dict]:
    """Return posts in a thread, newest first. only_active filters out expired posts."""
    limit = max(1, min(int(limit), 200))
    pool = await get_pool()
    async with pool.acquire() as conn:
        thread_row = await conn.fetchrow("SELECT id FROM thread WHERE slug=$1", thread_slug)
        if not thread_row:
            return []
        clauses = ["thread_id = $1"]
        params: list = [thread_row["id"]]
        if before_id is not None:
            clauses.append(f"id < ${len(params)+1}")
            params.append(int(before_id))
        if since_id is not None:
            clauses.append(f"id > ${len(params)+1}")
            params.append(int(since_id))
        if author:
            clauses.append(f"author = ${len(params)+1}")
            params.append(author)
        if only_active:
            clauses.append("(expires_at IS NULL OR expires_at > NOW())")
        where = " AND ".join(clauses)
        params.append(limit)
        rows = await conn.fetch(
            f"""SELECT id, thread_id, author, author_kind, posted_at, title, body, meta,
                       parent_post_id, expires_at
                FROM post
                WHERE {where}
                ORDER BY posted_at DESC
                LIMIT ${len(params)}""",
            *params,
        )
        return [dict(r) for r in rows]


async def search_posts(
    query: str,
    thread_slug: Optional[str] = None,
    author: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """ILIKE search on post title/body. Future: full-text via tsvector."""
    if not query or not query.strip():
        return []
    limit = max(1, min(int(limit), 200))
    pool = await get_pool()
    async with pool.acquire() as conn:
        clauses = ["(p.title ILIKE $1 OR p.body ILIKE $1)"]
        params: list = [f"%{query}%"]
        if thread_slug:
            clauses.append(f"t.slug = ${len(params)+1}")
            params.append(thread_slug)
        if author:
            clauses.append(f"p.author = ${len(params)+1}")
            params.append(author)
        where = " AND ".join(clauses)
        params.append(limit)
        rows = await conn.fetch(
            f"""SELECT p.id, t.slug AS thread_slug, p.author, p.author_kind,
                       p.posted_at, p.title, p.body, p.meta, p.expires_at
                FROM post p JOIN thread t ON t.id = p.thread_id
                WHERE {where}
                ORDER BY p.posted_at DESC
                LIMIT ${len(params)}""",
            *params,
        )
        return [dict(r) for r in rows]


async def mark_digest_telegram_sent(agent_name: str, trading_date: str) -> None:
    trading_date_dt = date.fromisoformat(trading_date) if isinstance(trading_date, str) else trading_date
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE agent_evening_digests
               SET telegram_sent_at = NOW()
               WHERE agent_name=$1 AND trading_date=$2::date""",
            agent_name, trading_date_dt,
        )


# ── Agent conviction views (sector-shard architecture) ───────────────────────

_VALID_CONVICTION_DIRECTIONS = {"long", "short", "flat"}


async def upsert_conviction(
    agent_name: str,
    symbol: str,
    direction: str,
    conviction: float,
    expected_return_pct: Optional[float] = None,
    time_to_target_days: Optional[int] = None,
    rationale: Optional[str] = None,
    model_inputs: Optional[dict] = None,
    expires_in_hours: int = 4,
) -> int:
    """Upsert one conviction row keyed on (agent_name, symbol). Most recent wins.
    direction='flat' with conviction=0 is the canonical 'I have no view' submission."""
    if direction not in _VALID_CONVICTION_DIRECTIONS:
        raise ValueError(f"direction must be one of {_VALID_CONVICTION_DIRECTIONS}, got {direction!r}")
    if conviction < 0:
        raise ValueError(f"conviction must be >= 0, got {conviction}")
    if direction == "flat" and conviction != 0:
        raise ValueError("flat direction requires conviction == 0")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO agent_conviction
                 (agent_name, symbol, direction, conviction,
                  expected_return_pct, time_to_target_days,
                  rationale, model_inputs, expires_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,
                       NOW() + ($9 || ' hours')::interval)
               ON CONFLICT (agent_name, symbol) DO UPDATE SET
                 direction           = EXCLUDED.direction,
                 conviction          = EXCLUDED.conviction,
                 expected_return_pct = EXCLUDED.expected_return_pct,
                 time_to_target_days = EXCLUDED.time_to_target_days,
                 rationale           = EXCLUDED.rationale,
                 model_inputs        = EXCLUDED.model_inputs,
                 submitted_at        = NOW(),
                 expires_at          = NOW() + ($9 || ' hours')::interval
               RETURNING id""",
            agent_name, symbol.upper(), direction, conviction,
            expected_return_pct, time_to_target_days, rationale,
            json.dumps(model_inputs) if model_inputs is not None else None,
            str(expires_in_hours),
        )
        return int(row["id"])


async def clear_agent_convictions(agent_name: str) -> int:
    """Delete all rows for this agent. Used at start of every review so the
    agent re-publishes a fresh slate (no stale views carried forward)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM agent_conviction WHERE agent_name=$1",
            agent_name,
        )
        # asyncpg returns "DELETE n" — parse the count.
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0


async def get_agent_active_convictions(agent_name: str) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, symbol, direction, conviction, expected_return_pct,
                      time_to_target_days, rationale, submitted_at, expires_at
               FROM agent_conviction
               WHERE agent_name=$1 AND expires_at > NOW() AND conviction > 0
               ORDER BY conviction DESC""",
            agent_name,
        )
        return [dict(r) for r in rows]


async def get_active_convictions() -> list[dict]:
    """All non-expired non-zero conviction rows across all agents.
    Mike-only consumer (the allocator)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, agent_name, symbol, direction, conviction,
                      expected_return_pct, time_to_target_days, rationale,
                      submitted_at, expires_at
               FROM agent_conviction
               WHERE expires_at > NOW() AND conviction > 0
               ORDER BY symbol, conviction DESC"""
        )
        return [dict(r) for r in rows]


async def get_consolidated_view() -> dict:
    """Aggregate active convictions by symbol. Returns:
       {symbol: {long_sum, short_sum, net, contributors: [{agent, direction, conviction}, ...]}}
    Mike-only consumer."""
    rows = await get_active_convictions()
    out: dict[str, dict] = {}
    for r in rows:
        sym = r["symbol"]
        bucket = out.setdefault(sym, {
            "symbol": sym, "long_sum": 0.0, "short_sum": 0.0,
            "net": 0.0, "contributors": [],
        })
        c = float(r["conviction"])
        if r["direction"] == "long":
            bucket["long_sum"] += c
            bucket["net"] += c
        elif r["direction"] == "short":
            bucket["short_sum"] += c
            bucket["net"] -= c
        bucket["contributors"].append({
            "agent": r["agent_name"],
            "direction": r["direction"],
            "conviction": c,
            "expected_return_pct": float(r["expected_return_pct"]) if r["expected_return_pct"] is not None else None,
            "time_to_target_days": r["time_to_target_days"],
            "rationale": r["rationale"],
        })
    return out


# ── Allocation decisions + P&L attribution ───────────────────────────────────

async def record_allocation_decision(
    nav_at_decision: float,
    target_weights: dict,
    contributing_views: dict,
    orders_placed: Optional[dict] = None,
    notes: Optional[str] = None,
) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO allocation_decision
                 (nav_at_decision, target_weights_json, contributing_views_json,
                  orders_placed_json, notes)
               VALUES ($1, $2::jsonb, $3::jsonb, $4::jsonb, $5)
               RETURNING id""",
            nav_at_decision,
            json.dumps(target_weights),
            json.dumps(contributing_views),
            json.dumps(orders_placed) if orders_placed is not None else None,
            notes,
        )
        return int(row["id"])


async def update_allocation_orders(decision_id: int, orders_placed: dict) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE allocation_decision SET orders_placed_json=$1::jsonb WHERE id=$2",
            json.dumps(orders_placed), decision_id,
        )


async def record_pnl_attribution(
    decision_id: int,
    agent_name: str,
    symbol: str,
    attribution_share: float,
    fill_id: Optional[int] = None,
) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO agent_pnl_attribution
                 (decision_id, agent_name, symbol, attribution_share, fill_id)
               VALUES ($1, $2, $3, $4, $5)
               RETURNING id""",
            decision_id, agent_name, symbol, attribution_share, fill_id,
        )
        return int(row["id"])


async def get_agent_pnl_attribution(
    agent_name: str,
    since: Optional[str] = None,
) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if since:
            rows = await conn.fetch(
                """SELECT id, fill_id, decision_id, symbol, attribution_share,
                          attributed_pnl, decided_at
                   FROM agent_pnl_attribution
                   WHERE agent_name=$1 AND decided_at >= $2::timestamptz
                   ORDER BY decided_at DESC""",
                agent_name, since,
            )
        else:
            rows = await conn.fetch(
                """SELECT id, fill_id, decision_id, symbol, attribution_share,
                          attributed_pnl, decided_at
                   FROM agent_pnl_attribution
                   WHERE agent_name=$1
                   ORDER BY decided_at DESC LIMIT 200""",
                agent_name,
            )
        return [dict(r) for r in rows]


# ── Sector story / archival ──────────────────────────────────────────────────
# Don't just purge old rows — summarize first. Each Saturday the
# sector-archivist skill walks every agent, generates a narrative chapter
# covering the prior period's closed theses + conviction history + attributed
# P&L, writes it to sector_story, then prunes the source rows. Future morning
# reviews read the latest stories so each agent retains a continuous arc.

async def get_archive_payload(agent_name: str, before: str) -> dict:
    """Aggregate everything-old for one agent up to (and including) `before`
    (ISO date). Used by sector-archivist to draft the narrative.

    Returns dict with: closed_theses, conviction_history (last seen per
    symbol), attribution_summary, last_period_end (most recent story's end
    date, so chapters chain without gaps).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        last_end = await conn.fetchval(
            """SELECT MAX(period_end) FROM sector_story WHERE agent_name=$1""",
            agent_name,
        )
        since_clause = "AND created_at > $3::timestamptz" if last_end else ""
        params = [agent_name, before]
        if last_end:
            params.append(f"{last_end.isoformat()}T00:00:00+00:00")

        theses = await conn.fetch(
            f"""SELECT id, created_at, kind, title, body, status,
                       resolution_note, resolved_at, verify_by
                FROM agent_thesis
                WHERE agent_name=$1
                  AND status IN ('confirmed','wrong','superseded')
                  AND resolved_at <= $2::timestamptz
                  {since_clause}
                ORDER BY resolved_at""",
            *params,
        )

        # Conviction history is replaced on each review (UNIQUE agent×symbol),
        # so "history" here is the most recent expired snapshot per symbol.
        convs = await conn.fetch(
            """SELECT symbol, direction, conviction, expected_return_pct,
                      time_to_target_days, rationale, submitted_at
               FROM agent_conviction
               WHERE agent_name=$1
                 AND expires_at <= $2::timestamptz""",
            agent_name, before,
        )

        attr = await conn.fetch(
            """SELECT symbol,
                      COUNT(*) AS fills,
                      SUM(attribution_share) AS share_total,
                      SUM(attributed_pnl) AS pnl_total
               FROM agent_pnl_attribution
               WHERE agent_name=$1
                 AND decided_at <= $2::timestamptz
               GROUP BY symbol
               ORDER BY pnl_total DESC NULLS LAST""",
            agent_name, before,
        )

        return {
            "agent_name": agent_name,
            "last_period_end": last_end.isoformat() if last_end else None,
            "before": before,
            "closed_theses": [dict(r) for r in theses],
            "expired_convictions": [dict(r) for r in convs],
            "attribution_summary": [dict(r) for r in attr],
        }


async def insert_sector_story(
    agent_name: str,
    period_start: str,
    period_end: str,
    narrative: str,
    stats: Optional[dict] = None,
    rows_archived: Optional[dict] = None,
) -> int:
    period_start_dt = date.fromisoformat(period_start) if isinstance(period_start, str) else period_start
    period_end_dt = date.fromisoformat(period_end) if isinstance(period_end, str) else period_end
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO sector_story
                 (agent_name, period_start, period_end, narrative, stats_json, rows_archived)
               VALUES ($1, $2::date, $3::date, $4, $5::jsonb, $6::jsonb)
               ON CONFLICT (agent_name, period_end) DO UPDATE
                 SET narrative = EXCLUDED.narrative,
                     stats_json = EXCLUDED.stats_json,
                     rows_archived = EXCLUDED.rows_archived,
                     created_at = NOW()
               RETURNING id""",
            agent_name, period_start_dt, period_end_dt, narrative,
            json.dumps(stats) if stats is not None else None,
            json.dumps(rows_archived) if rows_archived is not None else None,
        )
        return int(row["id"])


async def get_sector_stories(
    agent_name: str,
    limit: int = 8,
) -> list[dict]:
    """Return the most recent `limit` chapters for one agent, oldest-first
    (so when the agent reads them they tell the story in order)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, period_start, period_end, narrative, stats_json, created_at
               FROM (
                 SELECT id, period_start, period_end, narrative, stats_json, created_at
                 FROM sector_story
                 WHERE agent_name=$1
                 ORDER BY period_end DESC
                 LIMIT $2
               ) sub
               ORDER BY period_end ASC""",
            agent_name, limit,
        )
        return [dict(r) for r in rows]


async def prune_archived_rows(agent_name: str, before: str) -> dict:
    """Delete the rows that the latest sector_story for this agent has
    already absorbed. Only deletes:
      • closed agent_thesis rows resolved <= before
      • expired agent_conviction rows
      • agent_pnl_attribution rows decided <= before

    Caller must have just written a sector_story covering this window —
    `before` is the story's period_end. Returns row counts."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        t = await conn.execute(
            """DELETE FROM agent_thesis
               WHERE agent_name=$1
                 AND status IN ('confirmed','wrong','superseded')
                 AND resolved_at <= $2::timestamptz""",
            agent_name, before,
        )
        c = await conn.execute(
            """DELETE FROM agent_conviction
               WHERE agent_name=$1 AND expires_at <= $2::timestamptz""",
            agent_name, before,
        )
        p = await conn.execute(
            """DELETE FROM agent_pnl_attribution
               WHERE agent_name=$1 AND decided_at <= $2::timestamptz""",
            agent_name, before,
        )

        def _n(s: str) -> int:
            try:
                return int(s.split()[-1])
            except Exception:
                return 0

        return {
            "theses_deleted": _n(t),
            "convictions_deleted": _n(c),
            "attributions_deleted": _n(p),
        }


async def prune_global_noise(news_days: int = 14, audit_days: int = 30) -> dict:
    """Pure-noise pruning that doesn't need narrative archival: stale news
    headlines (re-fetched on demand), old audit_log + tool_calls (debug
    trail, not analysis substrate). Run alongside sector-archivist."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.execute(
            f"DELETE FROM news_items WHERE fetched_at < (NOW() - INTERVAL '{int(news_days)} days')::text"
        )
        a = await conn.execute(
            f"DELETE FROM audit_log WHERE created_at < (NOW() - INTERVAL '{int(audit_days)} days')::text"
        )
        tc = await conn.execute(
            f"DELETE FROM tool_calls WHERE created_at < (NOW() - INTERVAL '{int(audit_days)} days')::text"
        )

        def _n(s: str) -> int:
            try:
                return int(s.split()[-1])
            except Exception:
                return 0

        return {
            "news_deleted": _n(n),
            "audit_deleted": _n(a),
            "tool_calls_deleted": _n(tc),
        }
