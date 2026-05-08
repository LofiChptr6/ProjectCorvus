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
    realized_pnl: Optional[float] = None,
    db_path: str = DB_PATH,
) -> Optional[int]:
    """Insert a fill row. Returns the inserted id, or the existing id if a
    row with the same ibkr_exec_id already exists. Caller (the IBKR daemon
    `_on_fill`) uses the id to write per-agent ledger events."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO fills
               (ibkr_exec_id, order_id, agent_name, filled_at, symbol, action,
                quantity, fill_price, commission, exchange, mode, realized_pnl)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
               ON CONFLICT (ibkr_exec_id) DO NOTHING
               RETURNING id""",
            ibkr_exec_id, order_id, agent_name, filled_at,
            symbol, action, quantity, fill_price, commission, exchange, mode,
            realized_pnl,
        )
        if row:
            return int(row["id"])
        # Row already existed (re-delivered fill event); look up the id
        existing = await conn.fetchrow(
            "SELECT id FROM fills WHERE ibkr_exec_id = $1", ibkr_exec_id,
        )
        return int(existing["id"]) if existing else None


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

async def get_pnl_summary(
    agent_name: Optional[str] = None,
    period: str = "today",
    db_path: str = DB_PATH,
) -> list[dict]:
    """Realized P&L summary derived from `agent_ledger` events. Returns rows
    of {agent_name, trade_date, total_pnl, num_fills}. `total_pnl` here is
    realized only — for combined (realized + unrealized) callers should use
    `reporting.agent_pnl.get_pnl_combined()` which reads `agent_state`.

    Periods are window starts: 'today' | 'week' | 'month' | 'all'.
    """
    from datetime import date, timedelta
    today = date.today()
    params: list[Any] = []
    i = 1
    if period == "today":
        date_clause = f"booked_at::date = ${i}"
        params.append(today)
        i += 1
    elif period == "week":
        date_clause = f"booked_at::date >= ${i}"
        params.append(today - timedelta(days=7))
        i += 1
    elif period == "month":
        date_clause = f"booked_at::date >= ${i}"
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
               (booked_at::date)::text AS trade_date,
               COALESCE(SUM(realized_pnl), 0)::float8 AS total_pnl,
               COUNT(*)::int AS num_fills
        FROM agent_ledger
        WHERE event IN ('RETURN','DIVIDEND')
          AND realized_pnl IS NOT NULL
          AND {date_clause} {agent_clause}
        GROUP BY agent_name, booked_at::date
        ORDER BY booked_at::date DESC, agent_name
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
    *,
    url: Optional[str] = None,
    body: Optional[str] = None,
    sentiment: Optional[str] = None,
    channels: Optional[list[str]] = None,
    published_at: Optional[str] = None,
    db_path: str = DB_PATH,
) -> None:
    """Insert a news item; ON CONFLICT (article_id) DO NOTHING so reruns are safe.

    `published_at` accepts an ISO-8601 string ('2026-05-08T20:39:04Z' style) and
    is parsed to datetime here — asyncpg won't auto-cast strings to TIMESTAMPTZ.
    """
    pub_dt = None
    if published_at:
        try:
            # fromisoformat handles +00:00 but trips on a literal 'Z' suffix until 3.11+;
            # normalize defensively.
            pub_dt = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pub_dt = None
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO news_items
                (fetched_at, symbol, headline, article_id, provider,
                 url, body, sentiment, channels, published_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (article_id) WHERE article_id IS NOT NULL DO NOTHING
            """,
            _now(), symbol, headline, article_id, provider,
            url, body, sentiment, channels or None,
            pub_dt,
        )


async def get_recent_news(
    symbol: Optional[str] = None, limit: int = 10, db_path: str = DB_PATH
) -> list[dict]:
    """Return news items newest-first. Sort by published_at when present, fetched_at otherwise."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if symbol:
            rows = await conn.fetch(
                """
                SELECT id, fetched_at, symbol, headline, article_id, provider,
                       url, body, sentiment, channels, published_at
                FROM news_items
                WHERE symbol=$1
                ORDER BY COALESCE(published_at::text, fetched_at) DESC
                LIMIT $2
                """,
                symbol, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, fetched_at, symbol, headline, article_id, provider,
                       url, body, sentiment, channels, published_at
                FROM news_items
                ORDER BY COALESCE(published_at::text, fetched_at) DESC
                LIMIT $1
                """,
                limit,
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


async def get_recent_theses(
    agent_name: str,
    hours: int = 24,
    kinds: tuple[str, ...] = ("thesis", "observation"),
    limit: int = 8,
) -> list[dict]:
    """Most-recent agent_thesis rows for the day. Used by the evening slide
    composer to auto-aggregate fundamental thesis bullets at the top of the
    page. Filters by `kinds` (default excludes 'prediction' since those
    feed the open-questions panel, not the macro-thesis panel)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, created_at, kind, title, body, status
               FROM agent_thesis
               WHERE agent_name=$1
                 AND created_at >= NOW() - ($2 || ' hours')::interval
                 AND kind = ANY($3::text[])
               ORDER BY created_at DESC
               LIMIT $4""",
            agent_name, str(int(hours)), list(kinds), int(limit),
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
    expires_in_hours: int = 1,
    momentum_confirmed: Optional[bool] = None,
    stop_pct: Optional[float] = None,
) -> int:
    """Upsert one conviction row keyed on (agent_name, symbol). Most recent wins.
    direction='flat' with conviction=0 is the canonical 'I have no view' submission.

    Default TTL is 1 hour: hour-to-hour reconciliation. If the agent doesn't
    re-publish in the next review cycle, the conviction expires and the
    allocator closes the position. Agents wanting multi-session holds (overnight,
    weekend) must override with a larger expires_in_hours (e.g., 18 for overnight,
    72 for weekend). The market_hours risk check blocks SELLs outside RTH so
    premature off-hours expirations don't execute as actual trades.

    momentum_confirmed: agent's self-assertion for inverse-ETF buys. True ⇒ the
    underlying is already trending bearish (allocator auto-places). False ⇒ early
    entry ahead of confirmation (allocator queues for Telegram approval). None
    otherwise.

    stop_pct: optional defensive auto-flat trigger. If the position's unrealized
    return on this symbol falls below -stop_pct (e.g., 8 ⇒ -8%), the allocator
    treats this conviction as flat regardless of whether the agent re-publishes.
    Recommended for inverse-ETF longs (where decay compounds): 8% on 1× inverses,
    4% on ≥2× inverses. NULL ⇒ no stop."""
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
                  rationale, model_inputs, momentum_confirmed, expires_at,
                  stop_pct)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,
                       NOW() + ($10 || ' hours')::interval,
                       $11)
               ON CONFLICT (agent_name, symbol) DO UPDATE SET
                 direction           = EXCLUDED.direction,
                 conviction          = EXCLUDED.conviction,
                 expected_return_pct = EXCLUDED.expected_return_pct,
                 time_to_target_days = EXCLUDED.time_to_target_days,
                 rationale           = EXCLUDED.rationale,
                 model_inputs        = EXCLUDED.model_inputs,
                 momentum_confirmed  = EXCLUDED.momentum_confirmed,
                 stop_pct            = EXCLUDED.stop_pct,
                 submitted_at        = NOW(),
                 expires_at          = NOW() + ($10 || ' hours')::interval
               RETURNING id""",
            agent_name, symbol.upper(), direction, conviction,
            expected_return_pct, time_to_target_days, rationale,
            json.dumps(model_inputs) if model_inputs is not None else None,
            momentum_confirmed,
            str(expires_in_hours),
            stop_pct,
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
                      time_to_target_days, rationale, momentum_confirmed,
                      stop_pct, submitted_at, expires_at
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
                      momentum_confirmed, stop_pct, submitted_at, expires_at
               FROM agent_conviction
               WHERE expires_at > NOW() AND conviction > 0
               ORDER BY symbol, conviction DESC"""
        )
        return [dict(r) for r in rows]


# ── Agent forecasts (proof-of-work research) ─────────────────────────────────

def _derive_horizon(time_to_target_days: int) -> str:
    """Map time_to_target_days to one of the four canonical horizon buckets.
    Agents may also supply an explicit horizon in their forecast row to override
    this derivation."""
    if time_to_target_days <= 1:
        return "intraday"
    if time_to_target_days <= 5:
        return "near"
    if time_to_target_days <= 30:
        return "far"
    return "cycle"


async def upsert_forecasts_batch(
    agent_name: str,
    rows: list[dict],
    expires_in_hours: int = 2,
) -> dict:
    """Bulk-upsert forecasts per (agent_name, symbol, horizon). Each row dict
    carries `symbol`, `expected_return_pct`, `likelihood`, `time_to_target_days`,
    `method`, and optional `rationale` and `horizon`. `forecast_score` is
    computed server-side as expected_return_pct * likelihood / time_to_target_days.

    Multi-horizon: the same symbol may appear up to 4 times with different
    `time_to_target_days` (≤1d → 'intraday', 2-5d → 'near', 6-30d → 'far',
    31+d → 'cycle'). Each (agent, symbol, horizon) triple is an independent row.
    Pass `horizon` explicitly to override the auto-derived bucket.

    Default expires_in_hours is 2 — intraday forecasts should be refreshed
    every hourly review cycle; longer-horizon rows auto-expire if not re-submitted.

    Returns {inserted, errors[]}. Per-row failures (validation, type errors)
    are collected and returned alongside the success count rather than aborting
    the whole batch — partial inserts are fine since each upsert is atomic."""
    if not rows:
        return {"inserted": 0, "errors": []}

    prepared: list[tuple] = []
    errors: list[dict] = []
    for r in rows:
        try:
            sym = str(r["symbol"]).upper()
            er = float(r["expected_return_pct"])
            lk = float(r["likelihood"])
            ttd = int(r["time_to_target_days"])
            method = str(r.get("method") or "").strip()
            rationale = r.get("rationale")
            if not (0.0 <= lk <= 1.0):
                raise ValueError(f"likelihood must be in [0,1], got {lk}")
            if ttd <= 0:
                raise ValueError(f"time_to_target_days must be > 0, got {ttd}")
            if not method:
                raise ValueError("method is required (free-text describing source)")
            score = (er * lk) / ttd
            horizon = str(r.get("horizon") or _derive_horizon(ttd))
            if horizon not in {"intraday", "near", "far", "cycle"}:
                raise ValueError(f"horizon must be one of intraday/near/far/cycle, got {horizon!r}")
            prepared.append((agent_name, sym, horizon, er, lk, ttd, score, method, rationale))
        except (KeyError, ValueError, TypeError) as exc:
            errors.append({"row": r, "error": f"{type(exc).__name__}: {exc}"})

    if not prepared:
        return {"inserted": 0, "errors": errors}

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """INSERT INTO agent_forecast
                     (agent_name, symbol, horizon, expected_return_pct, likelihood,
                      time_to_target_days, forecast_score, method, rationale,
                      expires_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,
                           NOW() + ($10 || ' hours')::interval)
                   ON CONFLICT (agent_name, symbol, horizon) DO UPDATE SET
                     expected_return_pct = EXCLUDED.expected_return_pct,
                     likelihood          = EXCLUDED.likelihood,
                     time_to_target_days = EXCLUDED.time_to_target_days,
                     forecast_score      = EXCLUDED.forecast_score,
                     method              = EXCLUDED.method,
                     rationale           = EXCLUDED.rationale,
                     submitted_at        = NOW(),
                     expires_at          = NOW() + ($10 || ' hours')::interval""",
                [(*t, str(expires_in_hours)) for t in prepared],
            )
    return {"inserted": len(prepared), "errors": errors}


async def clear_agent_forecasts(agent_name: str, horizon: str | None = None) -> int:
    """Delete forecast rows for this agent. Called at start of every
    hourly review so the new batch fully replaces the prior hour's slate.

    Args:
        agent_name: The agent whose forecasts to clear.
        horizon: Optional — clear only this horizon ('intraday', 'near', 'far',
                 'cycle'). If None (default), clears all horizons. Useful when
                 an agent refreshes only their intraday forecasts each hour but
                 wants to preserve weekly cycle forecasts.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if horizon:
            result = await conn.execute(
                "DELETE FROM agent_forecast WHERE agent_name=$1 AND horizon=$2",
                agent_name, horizon,
            )
        else:
            result = await conn.execute(
                "DELETE FROM agent_forecast WHERE agent_name=$1",
                agent_name,
            )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0


async def get_agent_active_forecasts(agent_name: str, horizon: str | None = None) -> list[dict]:
    """Active (non-expired) forecast rows for one agent, sorted by
    horizon bucket then abs(forecast_score) descending.

    Args:
        agent_name: The agent to query.
        horizon: Optional filter — return only this horizon slice.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if horizon:
            rows = await conn.fetch(
                """SELECT id, symbol, horizon, expected_return_pct, likelihood,
                          time_to_target_days, forecast_score, method, rationale,
                          submitted_at, expires_at
                   FROM agent_forecast
                   WHERE agent_name=$1 AND horizon=$2 AND expires_at > NOW()
                   ORDER BY abs(forecast_score) DESC""",
                agent_name, horizon,
            )
        else:
            rows = await conn.fetch(
                """SELECT id, symbol, horizon, expected_return_pct, likelihood,
                          time_to_target_days, forecast_score, method, rationale,
                          submitted_at, expires_at
                   FROM agent_forecast
                   WHERE agent_name=$1 AND expires_at > NOW()
                   ORDER BY
                       CASE horizon WHEN 'intraday' THEN 0
                                    WHEN 'near'     THEN 1
                                    WHEN 'far'      THEN 2
                                    ELSE                 3 END,
                       abs(forecast_score) DESC""",
                agent_name,
            )
        return [dict(r) for r in rows]


async def get_active_forecasts() -> list[dict]:
    """All non-expired forecast rows across all agents, grouped by symbol then
    horizon bucket."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, agent_name, symbol, horizon, expected_return_pct,
                      likelihood, time_to_target_days, forecast_score,
                      method, rationale, submitted_at, expires_at
               FROM agent_forecast
               WHERE expires_at > NOW()
               ORDER BY symbol,
                   CASE horizon WHEN 'intraday' THEN 0
                                WHEN 'near'     THEN 1
                                WHEN 'far'      THEN 2
                                ELSE                 3 END,
                   abs(forecast_score) DESC"""
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


async def get_decision_id_for_order(order_id: int) -> Optional[int]:
    """Reverse-lookup the allocation_decision that produced a given order_id,
    by scanning orders_placed_json. Returns None if the order wasn't placed
    by the allocator (e.g. manual orders predating the sector-shard era)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id FROM allocation_decision
               WHERE orders_placed_json @> $1::jsonb
               ORDER BY id DESC LIMIT 1""",
            json.dumps([{"result": {"order_id": order_id}}]),
        )
        return int(row["id"]) if row else None


# ── Per-agent ledger writers + readers ───────────────────────────────────────
# Replaces record_pnl_attribution/add_attributed_pnl/record_holdings_snapshot
# under the double-entry redesign. See DESK_POLICY §0 for the model.

async def record_ledger_event(
    *,
    agent_name: str,
    symbol: str,
    event: str,
    qty: float,
    price_per_share: float,
    fill_id: Optional[int] = None,
    decision_id: Optional[int] = None,
    realized_pnl: Optional[float] = None,
    notes: Optional[str] = None,
    booked_at: Optional[Any] = None,
) -> int:
    """Append one row to `agent_ledger`. Returns the inserted row id.

    `booked_at` defaults to NOW() (live path). Backfill scripts pass an
    explicit datetime so historical events keep their real timestamps."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if booked_at is None:
            rid = await conn.fetchval(
                """INSERT INTO agent_ledger
                     (fill_id, decision_id, agent_name, symbol, event,
                      qty, price_per_share, realized_pnl, notes)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
                fill_id, decision_id, agent_name, symbol.upper(), event,
                float(qty), float(price_per_share),
                None if realized_pnl is None else float(realized_pnl),
                notes,
            )
        else:
            rid = await conn.fetchval(
                """INSERT INTO agent_ledger
                     (booked_at, fill_id, decision_id, agent_name, symbol, event,
                      qty, price_per_share, realized_pnl, notes)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id""",
                booked_at, fill_id, decision_id, agent_name, symbol.upper(), event,
                float(qty), float(price_per_share),
                None if realized_pnl is None else float(realized_pnl),
                notes,
            )
    return int(rid)


async def record_lend_for_fill(
    *,
    fill_id: Optional[int],
    decision_id: Optional[int],
    symbol: str,
    fill_qty: float,
    fill_price: float,
    agent_shares: list[tuple],
    booked_at: Optional[Any] = None,
) -> int:
    """One LEND row per agent who contributed to a BUY. `agent_shares` is
    the output of `meta_agent.allocator.split_attribution` — list of
    (agent_name, normalized_share). Each agent's lent qty = fill_qty × share.
    `booked_at` defaults to NOW(); backfill passes the historical fill time.
    Returns rows inserted."""
    if not agent_shares:
        return 0
    written = 0
    for agent, share in agent_shares:
        share_f = float(share)
        if share_f <= 0:
            continue
        lent_qty = float(fill_qty) * share_f
        if lent_qty <= 0:
            continue
        await record_ledger_event(
            agent_name=agent, symbol=symbol, event='LEND',
            qty=lent_qty, price_per_share=fill_price,
            fill_id=fill_id, decision_id=decision_id,
            booked_at=booked_at,
        )
        written += 1
    return written


async def record_return_for_fill(
    *,
    fill_id: Optional[int],
    decision_id: Optional[int],
    symbol: str,
    fill_qty: float,
    fill_price: float,
    booked_at: Optional[Any] = None,
) -> dict:
    """Pro-rata close: distribute a SELL fill's qty across all agents currently
    holding `symbol`, in proportion to their current open qty (as of right now,
    summed from agent_ledger). Each per-agent RETURN row carries:
        qty             = fill_qty × (agent_qty / total_held_qty)
        price_per_share = fill_price (sale price)
        realized_pnl    = qty × (fill_price − that_agent's_weighted_avg_cost)

    Weighted avg cost is invariant under pro-rata closes (see DESK_POLICY §0),
    so we recompute it from the agent's LEND/RETURN history each time.

    Returns {"rows_written": n, "agents": [...], "orphan_qty": qty_unclaimed}.
    If no agent currently holds the symbol, returns orphan_qty = fill_qty and
    writes nothing — the close stays on mike's book implicitly."""
    holders = await get_current_holders(symbol)  # {agent: qty}
    holders = {a: q for a, q in holders.items() if q > 1e-9}
    total = sum(holders.values())
    if total <= 0:
        return {"rows_written": 0, "agents": [], "orphan_qty": float(fill_qty)}

    fill_qty_f = float(fill_qty)
    fill_price_f = float(fill_price)
    written: list[dict] = []
    distributed = 0.0
    for agent, agent_qty in sorted(holders.items()):
        return_qty = fill_qty_f * (agent_qty / total)
        if return_qty <= 0:
            continue
        avg_cost = await get_agent_avg_cost(agent, symbol)
        realized = return_qty * (fill_price_f - avg_cost)
        await record_ledger_event(
            agent_name=agent, symbol=symbol, event='RETURN',
            qty=return_qty, price_per_share=fill_price_f,
            realized_pnl=realized,
            fill_id=fill_id, decision_id=decision_id,
            booked_at=booked_at,
        )
        written.append({
            "agent": agent, "qty": return_qty,
            "avg_cost": avg_cost, "realized_pnl": realized,
        })
        distributed += return_qty

    orphan = max(0.0, fill_qty_f - distributed)
    return {"rows_written": len(written), "agents": written, "orphan_qty": orphan}


async def get_current_holders(symbol: str) -> dict[str, float]:
    """Return {agent_name: net_qty} for current holders of `symbol`. Sum of
    LEND.qty − RETURN.qty per agent. Excludes DIVIDEND events (no qty change)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT agent_name,
                      SUM(CASE WHEN event = 'LEND'   THEN qty
                               WHEN event = 'RETURN' THEN -qty
                               ELSE 0 END)::float8 AS qty
               FROM agent_ledger
               WHERE UPPER(symbol) = $1
               GROUP BY agent_name
               HAVING SUM(CASE WHEN event = 'LEND'   THEN qty
                               WHEN event = 'RETURN' THEN -qty
                               ELSE 0 END) > 1e-9""",
            symbol.upper(),
        )
    return {r["agent_name"]: float(r["qty"]) for r in rows}


async def get_agent_avg_cost(agent_name: str, symbol: str) -> float:
    """Walk agent's LEND/RETURN events for `symbol` in time order, maintaining
    weighted average cost. Returns 0 if the agent has no open position.

    Pro-rata closes preserve avg_cost — but we still walk the full history
    so we're robust against any future event types or off-rule closes."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT event, qty::float8 AS qty, price_per_share::float8 AS price
               FROM agent_ledger
               WHERE agent_name = $1 AND UPPER(symbol) = $2
                 AND event IN ('LEND','RETURN')
               ORDER BY booked_at, id""",
            agent_name, symbol.upper(),
        )
    total_qty = 0.0
    total_cost = 0.0
    for r in rows:
        q = float(r["qty"])
        p = float(r["price"])
        if r["event"] == "LEND":
            total_qty += q
            total_cost += q * p
        else:  # RETURN
            avg = (total_cost / total_qty) if total_qty > 0 else 0.0
            total_qty -= q
            total_cost -= q * avg
            if total_qty < 1e-9:
                total_qty = 0.0
                total_cost = 0.0
    return (total_cost / total_qty) if total_qty > 1e-9 else 0.0


async def get_agent_holdings(
    agent_name: str,
    symbol: Optional[str] = None,
) -> dict[str, dict]:
    """Return {symbol: {qty, avg_cost, total_lent_cost, realized_pnl_to_date}}
    for one agent's open positions. If `symbol` provided, restricts to that
    one symbol (still returns dict, possibly empty).

    Uses ledger walk for avg_cost; uses one aggregate query for realized."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if symbol:
            sym_clause = "AND UPPER(symbol) = $2"
            args = [agent_name, symbol.upper()]
        else:
            sym_clause = ""
            args = [agent_name]
        rows = await conn.fetch(
            f"""SELECT UPPER(symbol) AS symbol,
                       SUM(CASE WHEN event = 'LEND'   THEN qty
                                WHEN event = 'RETURN' THEN -qty
                                ELSE 0 END)::float8 AS qty,
                       SUM(CASE WHEN event = 'LEND' THEN qty * price_per_share
                                ELSE 0 END)::float8 AS gross_lent_cost,
                       SUM(realized_pnl)::float8 AS realized
                FROM agent_ledger
                WHERE agent_name = $1 {sym_clause}
                GROUP BY UPPER(symbol)
                HAVING SUM(CASE WHEN event = 'LEND'   THEN qty
                                WHEN event = 'RETURN' THEN -qty
                                ELSE 0 END) > 1e-9""",
            *args,
        )
    out: dict[str, dict] = {}
    for r in rows:
        sym = r["symbol"]
        avg = await get_agent_avg_cost(agent_name, sym)
        qty = float(r["qty"])
        out[sym] = {
            "qty": qty,
            "avg_cost": avg,
            "open_cost": qty * avg,
            "realized_pnl_to_date": float(r["realized"] or 0.0),
        }
    return out


async def get_all_active_agents() -> list[str]:
    """Distinct agent_names that have any ledger activity. Used by the
    refresh script to know who to snapshot."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT agent_name FROM agent_ledger ORDER BY agent_name"
        )
    return [r["agent_name"] for r in rows]


async def record_agent_state(rows: list[dict]) -> int:
    """UPSERT one (agent, hour_bucket) row per dict. Each dict must contain:
    agent_name, realized_pnl, unrealized_pnl, total_pnl, open_cost,
    open_market_value, n_positions, positions_json (list)."""
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """INSERT INTO agent_state
                     (snapshot_at, agent_name, realized_pnl, unrealized_pnl,
                      total_pnl, open_cost, open_market_value, n_positions,
                      positions_json)
                   VALUES (NOW(),$1,$2,$3,$4,$5,$6,$7,$8::jsonb)
                   ON CONFLICT (agent_name, hour_bucket) DO UPDATE SET
                     snapshot_at       = EXCLUDED.snapshot_at,
                     realized_pnl      = EXCLUDED.realized_pnl,
                     unrealized_pnl    = EXCLUDED.unrealized_pnl,
                     total_pnl         = EXCLUDED.total_pnl,
                     open_cost         = EXCLUDED.open_cost,
                     open_market_value = EXCLUDED.open_market_value,
                     n_positions       = EXCLUDED.n_positions,
                     positions_json    = EXCLUDED.positions_json""",
                [
                    (
                        r["agent_name"],
                        float(r["realized_pnl"]),
                        float(r["unrealized_pnl"]),
                        float(r["total_pnl"]),
                        float(r["open_cost"]),
                        float(r["open_market_value"]),
                        int(r["n_positions"]),
                        json.dumps(r["positions_json"]),
                    )
                    for r in rows
                ],
            )
    return len(rows)


async def get_latest_agent_state(
    agent_name: Optional[str] = None,
) -> list[dict]:
    """Latest agent_state row per agent (the most recent hourly snapshot).
    If `agent_name` provided, restricts to that one agent."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if agent_name:
            rows = await conn.fetch(
                """SELECT DISTINCT ON (agent_name) *
                   FROM agent_state WHERE agent_name = $1
                   ORDER BY agent_name, snapshot_at DESC""",
                agent_name,
            )
        else:
            rows = await conn.fetch(
                """SELECT DISTINCT ON (agent_name) *
                   FROM agent_state
                   ORDER BY agent_name, snapshot_at DESC"""
            )
    out = []
    for r in rows:
        d = dict(r)
        # asyncpg returns JSONB as str; normalize
        if isinstance(d.get("positions_json"), str):
            d["positions_json"] = json.loads(d["positions_json"])
        out.append(d)
    return out


async def record_nav_log(
    desk_nav: float,
    cash_balance: float,
    decision_id: Optional[int] = None,
    source: str = "mike",
) -> int:
    """Append one anchor row to nav_log. Mike calls this once per rebalance
    with IBKR-canonical NAV + cash. The deterministic kanban refresh reads
    the latest row, then applies fills since `recorded_at` to compute
    current cash without touching IBKR. Returns the inserted row id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rid = await conn.fetchval(
            """INSERT INTO nav_log (decision_id, desk_nav, cash_balance, source)
               VALUES ($1, $2, $3, $4) RETURNING id""",
            decision_id, float(desk_nav), float(cash_balance), source,
        )
    return int(rid)


async def get_latest_nav_anchor() -> Optional[dict]:
    """Return the most recent nav_log row, or None if the table is empty.
    Used by scripts/refresh_kanban.py to anchor the cash leg."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, recorded_at, decision_id, desk_nav, cash_balance, source
               FROM nav_log ORDER BY recorded_at DESC LIMIT 1"""
        )
    return dict(row) if row else None


async def record_positions_anchor(
    positions: dict[str, float],
    decision_id: Optional[int] = None,
    source: str = "mike",
) -> int:
    """Append one positions anchor row. Mike calls this once per rebalance
    with IBKR-canonical {symbol: quantity}. The deterministic kanban refresh
    starts from the latest anchor and applies fills since `recorded_at` to
    derive current positions, sidestepping the fills-vs-IBKR drift we'd hit
    with a fills-only reconstruction. Returns the inserted row id."""
    snap = {
        str(sym).upper(): float(qty)
        for sym, qty in (positions or {}).items()
        if qty is not None
    }
    pool = await get_pool()
    async with pool.acquire() as conn:
        rid = await conn.fetchval(
            """INSERT INTO positions_anchor (decision_id, snapshot_json, source)
               VALUES ($1, $2::jsonb, $3) RETURNING id""",
            decision_id, json.dumps(snap), source,
        )
    return int(rid)


async def get_latest_positions_anchor() -> Optional[dict]:
    """Return the most recent positions_anchor row, or None if empty.
    Caller should treat `snapshot_json` as the IBKR-truth at `recorded_at`
    and apply any fills since then to compute current positions."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, recorded_at, decision_id, snapshot_json, source
               FROM positions_anchor ORDER BY recorded_at DESC LIMIT 1"""
        )
    if not row:
        return None
    out = dict(row)
    # asyncpg returns JSONB as a str; normalize to dict for callers.
    if isinstance(out.get("snapshot_json"), str):
        out["snapshot_json"] = json.loads(out["snapshot_json"])
    return out


async def get_agent_state_history(
    agent_name: str,
    lookback_hours: int = 24,
) -> list[dict]:
    """Return agent_state rows for one agent over the lookback window,
    ordered newest-first. Each row is a hourly snapshot of cumulative P&L
    and per-symbol detail (positions_json)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, snapshot_at, agent_name, realized_pnl, unrealized_pnl,
                      total_pnl, open_cost, open_market_value, n_positions,
                      positions_json
               FROM agent_state
               WHERE agent_name=$1
                 AND snapshot_at >= NOW() - ($2 || ' hours')::interval
               ORDER BY snapshot_at DESC""",
            agent_name, str(lookback_hours),
        )
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("positions_json"), str):
            d["positions_json"] = json.loads(d["positions_json"])
        out.append(d)
    return out


async def get_agent_ledger_events(
    agent_name: str,
    since: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """Per-agent ledger event log (LEND/RETURN/DIVIDEND), newest-first.
    Replaces the old get_agent_pnl_attribution reader."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if since:
            rows = await conn.fetch(
                """SELECT id, fill_id, decision_id, symbol, event,
                          qty::float8 AS qty, price_per_share::float8 AS price_per_share,
                          realized_pnl::float8 AS realized_pnl, booked_at, notes
                   FROM agent_ledger
                   WHERE agent_name=$1 AND booked_at >= $2::timestamptz
                   ORDER BY booked_at DESC""",
                agent_name, since,
            )
        else:
            rows = await conn.fetch(
                """SELECT id, fill_id, decision_id, symbol, event,
                          qty::float8 AS qty, price_per_share::float8 AS price_per_share,
                          realized_pnl::float8 AS realized_pnl, booked_at, notes
                   FROM agent_ledger
                   WHERE agent_name=$1
                   ORDER BY booked_at DESC LIMIT $2""",
                agent_name, limit,
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
                      COUNT(*) AS events,
                      SUM(CASE WHEN event = 'LEND'
                               THEN qty * price_per_share ELSE 0 END) AS gross_lent_cost,
                      SUM(realized_pnl) AS pnl_total
               FROM agent_ledger
               WHERE agent_name=$1
                 AND booked_at <= $2::timestamptz
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
            """DELETE FROM agent_ledger
               WHERE agent_name=$1 AND booked_at <= $2::timestamptz""",
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
            "ledger_events_deleted": _n(p),
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


# ── Parrot / Nori insight tools ───────────────────────────────────────────────

def _as_dt(s: str):
    """Convert ISO timestamp string → timezone-aware datetime for asyncpg."""
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _as_date(s: str):
    """Convert ISO date string → datetime.date for asyncpg DATE columns."""
    from datetime import date
    return date.fromisoformat(s[:10])


async def get_convictions_for_symbol(symbol: str) -> list[dict]:
    """Active conviction rows for a symbol across all agents (tools 1, 7)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT agent_name, direction, conviction::float8, expected_return_pct::float8,
                      time_to_target_days, rationale, submitted_at, expires_at
               FROM agent_conviction
               WHERE symbol = $1 AND expires_at > NOW() AND conviction > 0
               ORDER BY conviction DESC""",
            symbol.upper())
        return [dict(r) for r in rows]


async def get_symbol_fills(symbol: str, lookback_days: int = 30) -> list[dict]:
    """All fills for a symbol in the last N days, newest first (tools 1, 7)."""
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ibkr_exec_id, order_id, agent_name, filled_at,
                      action, quantity::float8, fill_price::float8,
                      commission::float8, realized_pnl::float8
               FROM fills
               WHERE symbol = $1 AND filled_at::timestamptz >= $2
               ORDER BY filled_at DESC LIMIT 200""",
            symbol.upper(), cutoff)
        return [dict(r) for r in rows]


async def get_symbol_pnl_summary(symbol: str, since: str | None = None) -> list[dict]:
    """Per-agent realized P&L for one symbol over the window (since `since`).
    Reads `agent_ledger` RETURN/DIVIDEND events."""
    params: list = [symbol.upper()]
    clause = ""
    if since:
        clause = "AND booked_at >= $2"
        params.append(_as_dt(since))
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT agent_name,
                       COUNT(*)::int                              AS total_events,
                       SUM(CASE WHEN event = 'LEND'   THEN qty
                                WHEN event = 'RETURN' THEN -qty
                                ELSE 0 END)::float8               AS net_qty_change,
                       COALESCE(SUM(realized_pnl), 0)::float8     AS realized_pnl
                FROM agent_ledger
                WHERE UPPER(symbol) = $1 {clause}
                GROUP BY agent_name
                ORDER BY realized_pnl DESC NULLS LAST""",
            *params)
        return [dict(r) for r in rows]


async def get_fills_window(
    since: str,
    until: str | None = None,
    agent_name: str | None = None,
) -> list[dict]:
    """Fills in a time range, optionally filtered by agent (tools 4, 8, 10)."""
    params: list = [_as_dt(since)]
    clauses = ["filled_at::timestamptz >= $1"]
    i = 2
    if until:
        clauses.append(f"filled_at::timestamptz <= ${i}")
        params.append(_as_dt(until))
        i += 1
    if agent_name:
        clauses.append(f"agent_name = ${i}")
        params.append(agent_name)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT id, ibkr_exec_id, order_id, agent_name, filled_at,
                       symbol, action, quantity::float8, fill_price::float8,
                       commission::float8, realized_pnl::float8, mode
                FROM fills WHERE {' AND '.join(clauses)}
                ORDER BY filled_at DESC LIMIT 1000""",
            *params)
        return [dict(r) for r in rows]


async def get_orders_window(
    since: str,
    agent_name: str | None = None,
) -> list[dict]:
    """Orders created since a timestamp, optionally filtered by agent (tools 4, 8)."""
    params: list = [_as_dt(since)]
    clause = ""
    if agent_name:
        clause = "AND agent_name = $2"
        params.append(agent_name)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT id, agent_name, created_at, symbol, action, order_type,
                       quantity::float8, limit_price::float8, status,
                       risk_approved, human_approved, rejection_reason, reasoning, mode
                FROM orders WHERE created_at::timestamptz >= $1 {clause}
                ORDER BY created_at DESC LIMIT 500""",
            *params)
        return [dict(r) for r in rows]


async def get_new_convictions_since(since: str) -> list[dict]:
    """Conviction rows submitted after a timestamp across all agents (tools 8, 9)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, agent_name, symbol, direction, conviction::float8,
                      expected_return_pct::float8, time_to_target_days,
                      rationale, submitted_at, expires_at
               FROM agent_conviction WHERE submitted_at >= $1
               ORDER BY submitted_at DESC""",
            _as_dt(since))
        return [dict(r) for r in rows]


async def get_new_theses_since(since: str) -> list[dict]:
    """Thesis rows created after a timestamp across all agents (tools 8, 9)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, agent_name, created_at, kind, title, body,
                      verify_by, status, parent_id
               FROM agent_thesis WHERE created_at >= $1
               ORDER BY created_at DESC LIMIT 200""",
            _as_dt(since))
        return [dict(r) for r in rows]


async def get_agent_evening_digest(
    agent_name: str,
    trading_date: str | None = None,
) -> dict | None:
    """Latest (or specific date) evening digest for one agent (tool 2)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if trading_date:
            row = await conn.fetchrow(
                "SELECT * FROM agent_evening_digests WHERE agent_name=$1 AND trading_date=$2::date",
                agent_name, trading_date)
        else:
            row = await conn.fetchrow(
                """SELECT * FROM agent_evening_digests WHERE agent_name=$1
                   ORDER BY trading_date DESC LIMIT 1""",
                agent_name)
        return dict(row) if row else None


async def get_pnl_attribution_by_symbol(
    since: str,
    until: str | None = None,
) -> list[dict]:
    """Symbol-level realized P&L rollup across all agents in a window.
    Sums realized_pnl from `agent_ledger` RETURN/DIVIDEND events."""
    params: list = [_as_dt(since)]
    clause = ""
    if until:
        clause = "AND booked_at <= $2"
        params.append(_as_dt(until))
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT UPPER(symbol) AS symbol,
                       COALESCE(SUM(realized_pnl), 0)::float8 AS total_pnl,
                       COUNT(DISTINCT fill_id)::int            AS fill_count,
                       array_agg(DISTINCT agent_name)          AS agents
                FROM agent_ledger
                WHERE booked_at >= $1 {clause}
                  AND event IN ('RETURN','DIVIDEND')
                  AND realized_pnl IS NOT NULL
                GROUP BY UPPER(symbol) ORDER BY total_pnl DESC NULLS LAST""",
            *params)
        return [dict(r) for r in rows]


async def get_pnl_attribution_by_agent(
    since: str,
    until: str | None = None,
) -> list[dict]:
    """Agent-level realized P&L rollup in a window."""
    params: list = [_as_dt(since)]
    clause = ""
    if until:
        clause = "AND booked_at <= $2"
        params.append(_as_dt(until))
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT agent_name,
                       COALESCE(SUM(realized_pnl), 0)::float8 AS total_pnl,
                       COUNT(DISTINCT fill_id)::int            AS fill_count
                FROM agent_ledger
                WHERE booked_at >= $1 {clause}
                  AND event IN ('RETURN','DIVIDEND')
                  AND realized_pnl IS NOT NULL
                GROUP BY agent_name ORDER BY total_pnl DESC NULLS LAST""",
            *params)
        return [dict(r) for r in rows]


async def get_fill_stats_by_agent_symbol(
    since: str,
    until: str | None = None,
) -> list[dict]:
    """Aggregate fills grouped by (agent_name, symbol) in a window (tool 4)."""
    params: list = [_as_dt(since)]
    clause = ""
    if until:
        clause = "AND filled_at::timestamptz <= $2"
        params.append(_as_dt(until))
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT agent_name, symbol,
                       COUNT(*)::int                                                AS fill_count,
                       SUM(quantity)::float8                                        AS total_qty,
                       AVG(fill_price)::float8                                      AS avg_price,
                       COALESCE(SUM(realized_pnl), 0)::float8                      AS realized_pnl,
                       SUM(CASE WHEN action='BUY'  THEN quantity ELSE 0 END)::float8 AS bought_qty,
                       SUM(CASE WHEN action='SELL' THEN quantity ELSE 0 END)::float8 AS sold_qty
                FROM fills WHERE filled_at::timestamptz >= $1 {clause}
                GROUP BY agent_name, symbol
                ORDER BY agent_name, realized_pnl DESC NULLS LAST""",
            *params)
        return [dict(r) for r in rows]


async def get_kill_switch_all_states() -> list[dict]:
    """All kill_switch rows — global + latest per-agent state (tool 5)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT ON (COALESCE(agent_name, ''))
                      id, agent_name, is_active, activated_at, activated_by, reason
               FROM kill_switch
               ORDER BY COALESCE(agent_name, ''), id DESC""")
        return [dict(r) for r in rows]


async def get_recent_allocation_decisions(limit: int = 10) -> list[dict]:
    """Most recent allocation_decision rows, metadata only (tools 5, 8)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, decided_at, nav_at_decision::float8, notes
               FROM allocation_decision ORDER BY decided_at DESC LIMIT $1""",
            limit)
        return [dict(r) for r in rows]


async def get_conviction_history_for_symbol(
    symbol: str,
    lookback_days: int = 60,
) -> list[dict]:
    """Ledger events for a symbol as a proxy for conviction-backed trade history.
    LEND/RETURN events are the new attribution audit trail."""
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT agent_name, booked_at, event,
                      qty::float8 AS qty,
                      price_per_share::float8 AS price_per_share,
                      realized_pnl::float8 AS realized_pnl,
                      decision_id
               FROM agent_ledger
               WHERE UPPER(symbol) = $1 AND booked_at >= $2
               ORDER BY booked_at DESC LIMIT 300""",
            symbol.upper(), cutoff)
        return [dict(r) for r in rows]


async def get_theses_due_all_agents(on_or_before: str) -> list[dict]:
    """All open prediction theses due for verification across all agents (tool 9)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, agent_name, created_at, kind, title, body, verify_by
               FROM agent_thesis
               WHERE status = 'open' AND verify_by IS NOT NULL
                 AND verify_by <= $1
               ORDER BY verify_by ASC, agent_name""",
            _as_date(on_or_before))
        return [dict(r) for r in rows]


async def get_convictions_expiring_soon(within_hours: int = 8) -> list[dict]:
    """Active convictions expiring within N hours (tool 9)."""
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) + timedelta(hours=within_hours)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT agent_name, symbol, direction, conviction::float8,
                      rationale, submitted_at, expires_at
               FROM agent_conviction
               WHERE expires_at > NOW()
                 AND expires_at <= $1
                 AND conviction > 0
               ORDER BY expires_at ASC""",
            cutoff)
        return [dict(r) for r in rows]


async def get_unattributed_fills(
    since: str,
    until: str | None = None,
) -> list[dict]:
    """Fills with no agent_ledger event — traded outside the allocator pipeline."""
    params: list = [_as_dt(since)]
    clause = ""
    if until:
        clause = "AND f.filled_at::timestamptz <= $2"
        params.append(_as_dt(until))
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT f.id, f.ibkr_exec_id, f.order_id, f.agent_name,
                       f.filled_at, f.symbol, f.action, f.quantity::float8,
                       f.fill_price::float8, f.commission::float8,
                       f.realized_pnl::float8, f.mode
                FROM fills f
                LEFT JOIN agent_ledger l ON l.fill_id = f.id
                WHERE f.filled_at::timestamptz >= $1 {clause}
                  AND l.id IS NULL
                ORDER BY f.filled_at DESC LIMIT 200""",
            *params)
        return [dict(r) for r in rows]
