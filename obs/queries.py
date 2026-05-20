"""Read-side queries for the obs dashboard.

The proxy writes audit_log + tool_calls; this module shapes that data for the
Streamlit views. We do NOT keep a long-lived asyncpg pool here — Streamlit
reruns the script top-to-bottom on every interaction and creates a fresh
asyncio loop each time, leaving any cached pool's connections bound to the
PREVIOUS (now-closed) loop. Cheaper to spin up a connection per query;
Streamlit's @st.cache_data wrapper around each query already deduplicates
across short windows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable, Awaitable

import asyncpg

log = logging.getLogger(__name__)


def _pg_dsn() -> str:
    host = os.environ.get("PG_HOST", "localhost")
    port = os.environ.get("PG_PORT", "5432")
    db = os.environ.get("PG_DATABASE", "trading")
    user = os.environ.get("PG_USER", "trading")
    pw = os.environ.get("PG_PASSWORD", "5369")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


async def _with_conn(fn: Callable[[asyncpg.Connection], Awaitable[Any]]) -> Any:
    conn = await asyncpg.connect(_pg_dsn(), command_timeout=10)
    try:
        return await fn(conn)
    finally:
        await conn.close()


# ── Recent runs by agent ─────────────────────────────────────────────────────


async def _list_recent_for_agent(agent: str, limit: int = 10) -> list[dict[str, Any]]:
    async def q(conn: asyncpg.Connection) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            """
            SELECT session_id, agent_name, routine, skill_name, created_at,
                   tool_rounds, prompt_tokens, completion_tokens, duration_ms,
                   finish_reason, error, request_index
            FROM audit_log
            WHERE agent_name = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            agent, limit,
        )
        return [dict(r) for r in rows]
    return await _with_conn(q)


def list_recent_for_agent(agent: str, limit: int = 10) -> list[dict[str, Any]]:
    return asyncio.run(_list_recent_for_agent(agent, limit))


# ── Recent skill invocations (one row per session_id) ────────────────────────


async def _list_recent_skill_invocations(limit: int = 50) -> list[dict[str, Any]]:
    async def q(conn: asyncpg.Connection) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            """
            SELECT
                session_id,
                MAX(agent_name)            AS agent_name,
                MAX(skill_name)            AS skill_name,
                MAX(routine)               AS routine,
                MIN(created_at)            AS started_at,
                MAX(created_at)            AS ended_at,
                SUM(tool_rounds)           AS total_tool_rounds,
                SUM(prompt_tokens)         AS prompt_tokens,
                SUM(completion_tokens)     AS completion_tokens,
                SUM(duration_ms)           AS duration_ms,
                COUNT(*)                   AS exchange_count,
                BOOL_OR(error IS NOT NULL) AS had_error,
                MAX(finish_reason)         AS finish_reason
            FROM audit_log
            WHERE skill_name IS NOT NULL OR routine IS NOT NULL
            GROUP BY session_id
            ORDER BY MIN(created_at) DESC
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]
    return await _with_conn(q)


def list_recent_skill_invocations(limit: int = 50) -> list[dict[str, Any]]:
    return asyncio.run(_list_recent_skill_invocations(limit))


# ── Per-session detail ───────────────────────────────────────────────────────


async def _get_session_exchanges(session_id: str) -> list[dict[str, Any]]:
    async def q(conn: asyncpg.Connection) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            """
            SELECT id, session_id, agent_name, routine, skill_name, request_index,
                   created_at, system_prompt, messages, tool_rounds, final_response,
                   finish_reason, duration_ms, prompt_tokens, completion_tokens, error,
                   thinking_block
            FROM audit_log
            WHERE session_id = $1
            ORDER BY request_index ASC, created_at ASC
            """,
            session_id,
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["messages_parsed"] = json.loads(d.get("messages") or "[]")
            except (TypeError, json.JSONDecodeError):
                d["messages_parsed"] = []
            out.append(d)
        return out
    return await _with_conn(q)


def get_session_exchanges(session_id: str) -> list[dict[str, Any]]:
    return asyncio.run(_get_session_exchanges(session_id))


async def _get_session_tool_calls(session_id: str) -> list[dict[str, Any]]:
    async def q(conn: asyncpg.Connection) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            """
            SELECT session_id, created_at, tool_round, tool_name, tool_input,
                   tool_output, duration_ms, error
            FROM tool_calls
            WHERE session_id = $1
            ORDER BY tool_round ASC, created_at ASC
            """,
            session_id,
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["tool_input_parsed"] = json.loads(d.get("tool_input") or "{}")
            except (TypeError, json.JSONDecodeError):
                d["tool_input_parsed"] = {}
            out.append(d)
        return out
    return await _with_conn(q)


def get_session_tool_calls(session_id: str) -> list[dict[str, Any]]:
    return asyncio.run(_get_session_tool_calls(session_id))


# ── Diff-tab helpers ─────────────────────────────────────────────────────────


async def _list_skills_for_agent(agent: str) -> list[str]:
    async def q(conn: asyncpg.Connection) -> list[str]:
        rows = await conn.fetch(
            """
            SELECT DISTINCT COALESCE(skill_name, routine) AS skill
            FROM audit_log
            WHERE agent_name = $1 AND COALESCE(skill_name, routine) IS NOT NULL
            ORDER BY 1
            """,
            agent,
        )
        return [r["skill"] for r in rows]
    return await _with_conn(q)


def list_skills_for_agent(agent: str) -> list[str]:
    return asyncio.run(_list_skills_for_agent(agent))


async def _list_sessions_for_skill(agent: str, skill: str, limit: int = 20) -> list[dict[str, Any]]:
    async def q(conn: asyncpg.Connection) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            """
            SELECT
                session_id,
                MIN(created_at)        AS started_at,
                SUM(duration_ms)       AS duration_ms,
                SUM(tool_rounds)       AS total_tool_rounds,
                SUM(prompt_tokens)     AS prompt_tokens,
                SUM(completion_tokens) AS completion_tokens,
                BOOL_OR(error IS NOT NULL) AS had_error
            FROM audit_log
            WHERE agent_name = $1 AND COALESCE(skill_name, routine) = $2
            GROUP BY session_id
            ORDER BY MIN(created_at) DESC
            LIMIT $3
            """,
            agent, skill, limit,
        )
        return [dict(r) for r in rows]
    return await _with_conn(q)


def list_sessions_for_skill(agent: str, skill: str, limit: int = 20) -> list[dict[str, Any]]:
    return asyncio.run(_list_sessions_for_skill(agent, skill, limit))


async def _list_known_agents() -> list[str]:
    async def q(conn: asyncpg.Connection) -> list[str]:
        rows = await conn.fetch("SELECT DISTINCT agent_name FROM audit_log ORDER BY 1")
        return [r["agent_name"] for r in rows]
    return await _with_conn(q)


def list_known_agents() -> list[str]:
    return asyncio.run(_list_known_agents())


# ── Live trace tab ────────────────────────────────────────────────────────────
# Reads exclusively from Postgres tables (positions_anchor, local_bars,
# fills, nav_log). The dashboard MUST NOT call massive_client directly —
# bars come from the local_bars cache the streamer maintains.

async def _latest_nav() -> dict[str, Any] | None:
    async def q(conn: asyncpg.Connection) -> dict[str, Any] | None:
        row = await conn.fetchrow(
            "SELECT recorded_at, desk_nav, cash_balance FROM nav_log "
            "ORDER BY recorded_at DESC LIMIT 1"
        )
        return dict(row) if row else None
    return await _with_conn(q)


def latest_nav() -> dict[str, Any] | None:
    return asyncio.run(_latest_nav())


async def _nav_history(days: int | None = 30) -> list[dict[str, Any]]:
    async def q(conn: asyncpg.Connection) -> list[dict[str, Any]]:
        if days is None:
            rows = await conn.fetch(
                "SELECT recorded_at, desk_nav, cash_balance FROM nav_log "
                "ORDER BY recorded_at ASC"
            )
        else:
            rows = await conn.fetch(
                """SELECT recorded_at, desk_nav, cash_balance
                   FROM nav_log
                   WHERE recorded_at > NOW() - ($1 || ' days')::interval
                   ORDER BY recorded_at ASC""",
                str(int(days)),
            )
        return [dict(r) for r in rows]
    return await _with_conn(q)


def nav_history(days: int | None = 30) -> list[dict[str, Any]]:
    """`days=None` returns the full nav_log (small table; safe to load)."""
    return asyncio.run(_nav_history(days))


async def _top_exposures(limit: int = 20) -> list[dict[str, Any]]:
    async def q(conn: asyncpg.Connection) -> list[dict[str, Any]]:
        # Latest positions_anchor snapshot. Each (symbol → qty) joined with the
        # newest local_bars close to derive mark + dollar exposure. Sorted by
        # |qty * mark| desc.
        anchor = await conn.fetchrow(
            "SELECT snapshot_json FROM positions_anchor "
            "ORDER BY recorded_at DESC LIMIT 1"
        )
        if not anchor:
            return []
        snap = anchor["snapshot_json"]
        if isinstance(snap, str):
            snap = json.loads(snap)
        if not snap:
            return []
        symbols = [s for s, q in (snap or {}).items()
                   if q is not None and abs(float(q)) > 1e-9]
        if not symbols:
            return []
        marks = await conn.fetch(
            """SELECT symbol, close, bar_time
               FROM (
                 SELECT symbol, close, bar_time,
                        ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY bar_time DESC) AS rn
                 FROM local_bars
                 WHERE symbol = ANY($1::text[]) AND interval='5min'
               ) t
               WHERE rn = 1""",
            symbols,
        )
        mark_map = {r["symbol"]: (float(r["close"]), r["bar_time"]) for r in marks}
        out: list[dict[str, Any]] = []
        for sym, qty in snap.items():
            try:
                qty_f = float(qty)
            except (TypeError, ValueError):
                continue
            if abs(qty_f) < 1e-9:
                continue
            mark, mark_at = mark_map.get(sym, (None, None))
            exposure = (mark or 0.0) * qty_f
            out.append({
                "symbol": sym,
                "qty": qty_f,
                "mark": mark,
                "mark_at": mark_at,
                "exposure": exposure,
            })
        out.sort(key=lambda r: abs(r["exposure"]), reverse=True)
        return out[:limit]
    return await _with_conn(q)


def top_exposures(limit: int = 20) -> list[dict[str, Any]]:
    return asyncio.run(_top_exposures(limit))


async def _recent_local_bars(symbol: str, n: int | None = 78,
                             days: int | None = None) -> list[dict[str, Any]]:
    async def q(conn: asyncpg.Connection) -> list[dict[str, Any]]:
        if days is not None:
            rows = await conn.fetch(
                """SELECT bar_time, open, high, low, close, volume
                   FROM local_bars
                   WHERE symbol=$1 AND interval='5min'
                     AND bar_time > NOW() - ($2 || ' days')::interval
                   ORDER BY bar_time ASC""",
                symbol.upper(), str(int(days)),
            )
            return [dict(r) for r in rows]
        rows = await conn.fetch(
            """SELECT bar_time, open, high, low, close, volume
               FROM local_bars
               WHERE symbol=$1 AND interval='5min'
               ORDER BY bar_time DESC LIMIT $2""",
            symbol.upper(), int(n or 78),
        )
        return [dict(r) for r in reversed(rows)]
    return await _with_conn(q)


def recent_local_bars(symbol: str, n: int | None = 78,
                      days: int | None = None) -> list[dict[str, Any]]:
    """If `days` is supplied, returns all bars in that window (bounded by
    local_bars' ~14-day retention). Else returns the last `n` bars."""
    return asyncio.run(_recent_local_bars(symbol, n=n, days=days))


async def _recent_daily_bars(symbol: str, days: int = 365) -> list[dict[str, Any]]:
    async def q(conn: asyncpg.Connection) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            f"""SELECT bar_date, open, high, low, close, volume
                FROM local_bars_daily
                WHERE symbol=$1
                  AND bar_date > (CURRENT_DATE - INTERVAL '{int(days)} days')
                ORDER BY bar_date ASC""",
            symbol.upper(),
        )
        return [dict(r) for r in rows]
    return await _with_conn(q)


def recent_daily_bars(symbol: str, days: int = 365) -> list[dict[str, Any]]:
    """Trailing daily bars from local_bars_daily — the 1Y per-ticker view used
    by the dashboard. Populated by scripts/ingest_daily_bars.py once per
    trading-day close."""
    return asyncio.run(_recent_daily_bars(symbol, days=days))


# ── Fill context bundle ──────────────────────────────────────────────────────
# Drives the Live Trace right-side panel: click a fill triangle → assemble
# every breadcrumb that tells the operator WHY this trade happened.
# Bundle shape documented at scripts/run_queue_worker.py top of file (no —
# at the get_fill_context call site in obs/dashboard.py).

async def _get_fill_context(symbol: str, filled_at_iso: str) -> dict | None:
    # asyncpg requires real datetimes for ::timestamptz parameters — coerce
    # the ISO string (which arrived through the Altair selection event).
    from datetime import datetime, timezone
    filled_at_dt = datetime.fromisoformat(filled_at_iso.replace("Z", "+00:00"))
    if filled_at_dt.tzinfo is None:
        filled_at_dt = filled_at_dt.replace(tzinfo=timezone.utc)

    async def q(conn: asyncpg.Connection) -> dict | None:
        # 1. Locate the fill row. Tolerate ±2s on the timestamp match — Altair
        #    round-trips ISO strings and microsecond fidelity is lossy.
        fill = await conn.fetchrow(
            """SELECT id, symbol, filled_at, action, quantity, fill_price,
                      order_id, agent_name
               FROM fills
               WHERE symbol=$1
                 AND filled_at::timestamptz BETWEEN
                       $2::timestamptz - INTERVAL '2 seconds' AND
                       $2::timestamptz + INTERVAL '2 seconds'
               ORDER BY filled_at LIMIT 1""",
            symbol.upper(), filled_at_dt,
        )
        if not fill:
            return None
        fill_d = dict(fill)

        # 2. Contributing agents from the ledger.
        ledger_rows = await conn.fetch(
            """SELECT agent_name, event, qty, price_per_share,
                      realized_pnl, decision_id, booked_at
               FROM agent_ledger
               WHERE fill_id=$1
               ORDER BY qty DESC""",
            fill_d["id"],
        )

        # 3. Optional: the allocator decision row that produced this fill.
        decision_d: dict | None = None
        decision_id = next(
            (r["decision_id"] for r in ledger_rows if r["decision_id"] is not None),
            None,
        )
        if decision_id:
            d = await conn.fetchrow(
                "SELECT id, decided_at, nav_at_decision, notes "
                "FROM allocation_decision WHERE id=$1",
                decision_id,
            )
            if d:
                decision_d = dict(d)

        # 4. For each contributing agent, gather conviction + theses + session
        #    + tool_calls. Heuristic for session lookup is documented inline.
        contributors: list[dict] = []
        for lr in ledger_rows:
            agent = lr["agent_name"]
            conviction = await conn.fetchrow(
                """SELECT direction, conviction, expected_return_pct,
                          time_to_target_days, rationale, model_inputs,
                          submitted_at, expires_at, momentum_confirmed,
                          stop_pct, session_id
                   FROM agent_conviction
                   WHERE agent_name=$1 AND symbol=$2
                     AND submitted_at <= $3
                   ORDER BY submitted_at DESC LIMIT 1""",
                agent, symbol.upper(), filled_at_dt,
            )

            theses = await conn.fetch(
                """SELECT id, kind, title, body, status, verify_by,
                          direction, entry_price, created_at, resolution_note,
                          resolved_at, resolution_source
                   FROM agent_thesis
                   WHERE agent_name=$1 AND primary_symbol=$2
                     AND status='open'
                   ORDER BY created_at DESC LIMIT 5""",
                agent, symbol.upper(),
            )

            # Session lookup: prefer the exact session_id stamped on the
            # conviction (after the audit recommendation landed); fall back
            # to the latest audit_log row for this agent at or before the
            # conviction's submitted_at when session_id is NULL (legacy rows).
            session = None
            session_id_from_conv = conviction["session_id"] if conviction else None
            if session_id_from_conv:
                session = await conn.fetchrow(
                    """SELECT session_id, agent_name, routine, skill_name,
                              created_at, system_prompt, thinking_block,
                              final_response, finish_reason, tool_rounds,
                              duration_ms, prompt_tokens, completion_tokens,
                              error, request_index
                       FROM audit_log
                       WHERE session_id=$1
                       ORDER BY request_index DESC, created_at DESC LIMIT 1""",
                    session_id_from_conv,
                )
            if not session:
                # Heuristic fallback for rows written before session_id existed.
                session_anchor = conviction["submitted_at"] if conviction else filled_at_dt
                session_anchor_iso = (
                    session_anchor.isoformat()
                    if hasattr(session_anchor, "isoformat") else str(session_anchor)
                )
                session = await conn.fetchrow(
                    """SELECT session_id, agent_name, routine, skill_name,
                              created_at, system_prompt, thinking_block,
                              final_response, finish_reason, tool_rounds,
                              duration_ms, prompt_tokens, completion_tokens,
                              error, request_index
                       FROM audit_log
                       WHERE agent_name=$1 AND created_at <= $2
                       ORDER BY created_at DESC LIMIT 1""",
                    agent, session_anchor_iso,
                )

            tool_calls: list[dict] = []
            if session:
                tc_rows = await conn.fetch(
                    """SELECT tool_round, tool_name, tool_input, tool_output,
                              duration_ms, error, created_at
                       FROM tool_calls
                       WHERE session_id=$1
                       ORDER BY tool_round ASC, id ASC""",
                    session["session_id"],
                )
                tool_calls = [dict(t) for t in tc_rows]

            contributors.append({
                "agent_name": agent,
                "ledger_event": lr["event"],
                "ledger_qty": float(lr["qty"]),
                "ledger_price": float(lr["price_per_share"]),
                "ledger_pnl": (float(lr["realized_pnl"])
                               if lr["realized_pnl"] is not None else None),
                "ledger_booked_at": lr["booked_at"],
                "conviction": dict(conviction) if conviction else None,
                "theses": [dict(t) for t in theses],
                "session": dict(session) if session else None,
                "tool_calls": tool_calls,
            })

        # Default: ledger-sourced attribution. Time-fallback path below
        # overrides this when the ledger came up empty.
        attribution_source = "ledger"

        # ── Time-fallback (Phase 2) ──────────────────────────────────────────
        # When no agent_ledger rows back this fill — pre-system fills, sector-
        # agents bypassing mike, or a yet-undiagnosed join miss — try to find
        # the nearest allocation_decision and synthesize a contributor list
        # from its contributing_views_json[symbol]. The panel renders these
        # with an "inferred" badge so the user knows attribution is reconstructed.
        if not contributors:
            nearby = await conn.fetchrow(
                """SELECT id, decided_at, nav_at_decision, notes,
                          contributing_views_json
                   FROM allocation_decision
                   WHERE decided_at BETWEEN $1::timestamptz - INTERVAL '90 seconds'
                                       AND $1::timestamptz + INTERVAL '90 seconds'
                   ORDER BY ABS(EXTRACT(EPOCH FROM (decided_at - $1::timestamptz)))
                   LIMIT 1""",
                filled_at_dt,
            )
            if nearby:
                cv = nearby["contributing_views_json"]
                if isinstance(cv, str):
                    try:
                        cv = json.loads(cv)
                    except json.JSONDecodeError:
                        cv = {}
                cv = cv or {}
                # The decision may have stored views under the *original* symbol
                # (e.g. "QQQ") when the held vehicle is the inverse (e.g. "SQQQ").
                # Look for a direct match first; if none, scan for any list of
                # views (the symbol mapping isn't reliably surfaced at this layer).
                views = cv.get(symbol.upper()) or cv.get(symbol)
                if not views:
                    for k, v in cv.items():
                        if v and isinstance(v, list):
                            # First non-empty list — best-effort guess.
                            views = v
                            break
                for v in (views or []):
                    contributors.append({
                        "agent_name": v.get("agent") or "unknown",
                        "ledger_event": None,
                        "ledger_qty": 0.0,
                        "ledger_price": float(fill_d["fill_price"] or 0.0),
                        "ledger_pnl": None,
                        "ledger_booked_at": None,
                        "conviction": None,
                        "theses": [],
                        "session": None,
                        "tool_calls": [],
                        "inferred_weight": float(v.get("weight") or 0.0),
                    })
                if contributors:
                    attribution_source = "inferred_by_time"
                # Even when the decision was found but had no symbol-matching views,
                # surface the decision itself so the panel can show *something*.
                if decision_d is None:
                    decision_d = {
                        "id": nearby["id"],
                        "decided_at": nearby["decided_at"],
                        "nav_at_decision": nearby["nav_at_decision"],
                        "notes": nearby["notes"],
                    }

        return {
            "fill": fill_d,
            "contributors": contributors,
            "decision": decision_d,
            "attribution_source": attribution_source,
        }
    return await _with_conn(q)


def get_fill_context(symbol: str, filled_at_iso: str) -> dict | None:
    """Assemble the full 'why did this trade happen' bundle for one fill.
    Used by the Live Trace right-side panel. See obs/dashboard.py:
    _render_fill_context_panel for the consumer."""
    return asyncio.run(_get_fill_context(symbol, filled_at_iso))


async def _recent_fills(symbol: str, since_hours: int = 24) -> list[dict[str, Any]]:
    async def q(conn: asyncpg.Connection) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            """SELECT filled_at::timestamptz AS filled_at,
                      action, quantity, fill_price
               FROM fills
               WHERE symbol=$1
                 AND filled_at::timestamptz > NOW() - ($2 || ' hours')::interval
               ORDER BY filled_at::timestamptz ASC""",
            symbol.upper(), str(int(since_hours)),
        )
        return [dict(r) for r in rows]
    return await _with_conn(q)


def recent_fills(symbol: str, since_hours: int = 24) -> list[dict[str, Any]]:
    return asyncio.run(_recent_fills(symbol, since_hours))


# ── Ops health: queue + streamer ─────────────────────────────────────────────

async def _ops_health() -> dict[str, Any]:
    async def q(conn: asyncpg.Connection) -> dict[str, Any]:
        by_status = await conn.fetch(
            "SELECT status, COUNT(*) AS n FROM agent_job GROUP BY status"
        )
        oldest_q = await conn.fetchval(
            "SELECT EXTRACT(EPOCH FROM (NOW() - MIN(enqueued_at))) "
            "FROM agent_job WHERE status='queued'"
        )
        intraday = await conn.fetchrow(
            "SELECT COUNT(*) AS rows, COUNT(DISTINCT symbol) AS syms, "
            "MAX(ingested_at) AS last_ingest, "
            "EXTRACT(EPOCH FROM (NOW() - MAX(ingested_at))) AS lag_s "
            "FROM local_bars"
        )
        daily = await conn.fetchrow(
            "SELECT COUNT(*) AS rows, COUNT(DISTINCT symbol) AS syms, "
            "MAX(ingested_at) AS last_ingest, "
            "EXTRACT(EPOCH FROM (NOW() - MAX(ingested_at))) AS lag_s "
            "FROM local_bars_daily"
        )
        active_workers = await conn.fetchval(
            "SELECT COUNT(DISTINCT worker_id) FROM agent_job "
            "WHERE status='running' AND worker_id IS NOT NULL"
        )
        recent_done = await conn.fetchval(
            "SELECT COUNT(*) FROM agent_job "
            "WHERE status='done' AND finished_at > NOW() - INTERVAL '1 hour'"
        )
        recent_failed = await conn.fetchval(
            "SELECT COUNT(*) FROM agent_job "
            "WHERE status='failed' AND finished_at > NOW() - INTERVAL '1 hour'"
        )
        return {
            "queue": {
                "by_status": {r["status"]: int(r["n"]) for r in by_status},
                "oldest_queued_age_s": float(oldest_q) if oldest_q else None,
                "active_workers": int(active_workers or 0),
                "done_1h": int(recent_done or 0),
                "failed_1h": int(recent_failed or 0),
            },
            "local_bars": {
                "rows": int(intraday["rows"] or 0),
                "symbols": int(intraday["syms"] or 0),
                "last_ingest": intraday["last_ingest"],
                "lag_s": float(intraday["lag_s"]) if intraday["lag_s"] is not None else None,
            },
            "local_bars_daily": {
                "rows": int(daily["rows"] or 0),
                "symbols": int(daily["syms"] or 0),
                "last_ingest": daily["last_ingest"],
                "lag_s": float(daily["lag_s"]) if daily["lag_s"] is not None else None,
            },
        }
    return await _with_conn(q)


def ops_health() -> dict[str, Any]:
    return asyncio.run(_ops_health())


async def _tool_error_summary(
    agent_name: str | None = None,
    since_hours: int = 24,
    min_errors: int = 1,
) -> list[dict[str, Any]]:
    async def q(conn: asyncpg.Connection) -> list[dict[str, Any]]:
        # tool_calls.created_at is TEXT (ISO); cast for the time window.
        # Join with audit_log to attribute (session_id → agent_name).
        where_agent = "AND al.agent_name = $2" if agent_name else ""
        params: list[Any] = [str(int(since_hours))]
        if agent_name:
            params.append(agent_name)
        rows = await conn.fetch(
            f"""SELECT al.agent_name,
                       tc.tool_name,
                       COUNT(*) AS total_calls,
                       SUM(CASE WHEN tc.error IS NOT NULL THEN 1 ELSE 0 END) AS error_calls,
                       MAX(tc.created_at) FILTER (WHERE tc.error IS NOT NULL) AS last_error_at,
                       (ARRAY_AGG(tc.error ORDER BY tc.id DESC)
                        FILTER (WHERE tc.error IS NOT NULL))[1] AS last_error_msg
                FROM tool_calls tc
                JOIN audit_log al ON al.session_id = tc.session_id
                WHERE tc.created_at::timestamptz > NOW() - ($1 || ' hours')::interval
                  {where_agent}
                GROUP BY al.agent_name, tc.tool_name
                HAVING SUM(CASE WHEN tc.error IS NOT NULL THEN 1 ELSE 0 END) >= {int(min_errors)}
                ORDER BY error_calls DESC, total_calls DESC
                LIMIT 100""",
            *params,
        )
        out = []
        for r in rows:
            d = dict(r)
            d["error_rate"] = (
                float(d["error_calls"]) / float(d["total_calls"])
                if d["total_calls"] else 0.0
            )
            # Truncate last_error_msg for readability — the full text is in
            # tool_calls.error if the operator wants to drill in.
            if d.get("last_error_msg"):
                d["last_error_msg"] = d["last_error_msg"][:300]
            out.append(d)
        return out
    return await _with_conn(q)


def tool_error_summary(
    agent_name: str | None = None,
    since_hours: int = 24,
    min_errors: int = 1,
) -> list[dict[str, Any]]:
    """Aggregated tool-call failures over the last N hours, grouped by
    (agent_name, tool_name). Surfaces systemic tool problems (e.g. "energy's
    get_news has failed 18/20 times in the last hour")."""
    return asyncio.run(_tool_error_summary(agent_name, since_hours, min_errors))


# ── Probabilistic-forecast skill (calibration aggregation) ──────────────────


async def _agent_skill_by_horizon(window_days: int = 30) -> list[dict[str, Any]]:
    """Aggregate calibration scores per (agent, horizon) over the window.

    Sharpe-of-skill: realized × sign(E[r]) is the per-row signed edge; its
    mean over std (annualized by √(252/horizon_days_avg) heuristically dropped
    here — the rank ordering across agents is what matters for the dashboard).
    """
    async def q(conn: asyncpg.Connection) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            """
            WITH scored AS (
                SELECT
                    agent_name, horizon,
                    realized_return_pct,
                    expected_return_pct,
                    score_logloss, score_brier, score_crps,
                    score_pinball05, score_pinball95,
                    SIGN(expected_return_pct) * realized_return_pct AS signed_edge
                FROM agent_forecast
                WHERE resolved_at IS NOT NULL
                  AND resolved_at > NOW() - ($1 || ' days')::interval
                  AND realized_return_pct IS NOT NULL
                  AND distribution IS NOT NULL
            )
            SELECT
                agent_name, horizon,
                COUNT(*)                              AS n,
                AVG(score_logloss)                    AS mean_logloss,
                AVG(score_brier)                      AS mean_brier,
                AVG(score_crps)                       AS mean_crps,
                AVG(score_pinball05)                  AS mean_pinball05,
                AVG(score_pinball95)                  AS mean_pinball95,
                AVG(signed_edge)                      AS mean_signed_edge,
                STDDEV_POP(signed_edge)               AS sd_signed_edge,
                CASE WHEN STDDEV_POP(signed_edge) > 0
                     THEN AVG(signed_edge) / STDDEV_POP(signed_edge)
                     ELSE 0 END                        AS sharpe_of_skill
            FROM scored
            GROUP BY agent_name, horizon
            ORDER BY agent_name, horizon
            """,
            str(window_days),
        )
        return [dict(r) for r in rows]
    return await _with_conn(q)


def agent_skill_by_horizon(window_days: int = 30) -> list[dict[str, Any]]:
    return asyncio.run(_agent_skill_by_horizon(window_days))


async def _model_skill_by_horizon(window_days: int = 30) -> list[dict[str, Any]]:
    """Same as agent_skill_by_horizon but grouped by model. Model is extracted
    from the distribution payload (`distribution->>'model'`)."""
    async def q(conn: asyncpg.Connection) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            """
            WITH scored AS (
                SELECT
                    agent_name,
                    COALESCE(distribution->>'model', 'unknown') AS model,
                    COALESCE(distribution->>'model_version', '?') AS model_version,
                    horizon,
                    realized_return_pct,
                    expected_return_pct,
                    score_logloss, score_brier, score_crps,
                    score_pinball05, score_pinball95,
                    SIGN(expected_return_pct) * realized_return_pct AS signed_edge
                FROM agent_forecast
                WHERE resolved_at IS NOT NULL
                  AND resolved_at > NOW() - ($1 || ' days')::interval
                  AND realized_return_pct IS NOT NULL
                  AND distribution IS NOT NULL
            )
            SELECT
                agent_name, model, model_version, horizon,
                COUNT(*)                              AS n,
                AVG(score_logloss)                    AS mean_logloss,
                AVG(score_brier)                      AS mean_brier,
                AVG(score_crps)                       AS mean_crps,
                AVG(score_pinball05)                  AS mean_pinball05,
                AVG(score_pinball95)                  AS mean_pinball95,
                AVG(signed_edge)                      AS mean_signed_edge,
                CASE WHEN STDDEV_POP(signed_edge) > 0
                     THEN AVG(signed_edge) / STDDEV_POP(signed_edge)
                     ELSE 0 END                        AS sharpe_of_skill
            FROM scored
            GROUP BY agent_name, model, model_version, horizon
            ORDER BY agent_name, model, horizon
            """,
            str(window_days),
        )
        return [dict(r) for r in rows]
    return await _with_conn(q)


def model_skill_by_horizon(window_days: int = 30) -> list[dict[str, Any]]:
    return asyncio.run(_model_skill_by_horizon(window_days))


async def _calibration_curve(
    agent: str | None = None,
    model: str | None = None,
    horizon: str | None = None,
    window_days: int = 30,
    n_buckets: int = 10,
) -> list[dict[str, Any]]:
    """Bin-level reliability diagram: for predictions in each predicted-prob
    bucket (e.g. p ∈ [0.3, 0.4)), what fraction of those bins actually
    realized? Returns one row per bucket with predicted_mid, realized_rate,
    n_predictions. Drops buckets with n < 5 — too noisy to interpret.

    Walks each resolved distribution: for every bin, attribute its predicted
    probability into the appropriate prob-bucket; if that bin equals the
    realized_bin_idx, count it as a hit. Reliability is hits / count per bucket.
    """
    bucket_edges = [i / n_buckets for i in range(n_buckets + 1)]
    async def q(conn: asyncpg.Connection) -> list[dict[str, Any]]:
        # Pull resolved rows with distribution + realized_bin_idx; bucket in
        # Python to keep SQL simple. Cap at 5000 rows.
        rows = await conn.fetch(
            """
            SELECT distribution, realized_bin_idx
            FROM agent_forecast
            WHERE resolved_at IS NOT NULL
              AND resolved_at > NOW() - ($1 || ' days')::interval
              AND distribution IS NOT NULL
              AND realized_bin_idx IS NOT NULL
              AND ($2::text IS NULL OR agent_name = $2)
              AND ($3::text IS NULL OR distribution->>'model' = $3)
              AND ($4::text IS NULL OR horizon = $4)
            LIMIT 5000
            """,
            str(window_days), agent, model, horizon,
        )
        return [dict(r) for r in rows]
    raw = await _with_conn(q)

    # Bucket
    hits = [0] * n_buckets
    counts = [0] * n_buckets
    for r in raw:
        dist = r["distribution"]
        if isinstance(dist, str):
            try:
                dist = json.loads(dist)
            except json.JSONDecodeError:
                continue
        bins = dist.get("bins") or []
        realized_idx = int(r["realized_bin_idx"])
        for i, b in enumerate(bins):
            p = float(b.get("p", 0.0))
            if p <= 0:
                continue
            bucket = min(int(p * n_buckets), n_buckets - 1)
            counts[bucket] += 1
            if i == realized_idx:
                hits[bucket] += 1

    out = []
    for k in range(n_buckets):
        if counts[k] < 5:
            continue
        out.append({
            "predicted_low":  bucket_edges[k],
            "predicted_high": bucket_edges[k + 1],
            "predicted_mid":  (bucket_edges[k] + bucket_edges[k + 1]) / 2,
            "realized_rate":  hits[k] / counts[k],
            "n_predictions":  counts[k],
        })
    return out


def calibration_curve(
    agent: str | None = None,
    model: str | None = None,
    horizon: str | None = None,
    window_days: int = 30,
    n_buckets: int = 10,
) -> list[dict[str, Any]]:
    return asyncio.run(
        _calibration_curve(agent=agent, model=model, horizon=horizon,
                           window_days=window_days, n_buckets=n_buckets)
    )


# ── Per-(symbol × agent) live-trace queries ─────────────────────────────────


async def _agents_with_signal_on_symbol(
    symbol: str, since_days: int = 14,
) -> list[str]:
    """Return the agent_names that have any signal on `symbol` in the window:
       conviction row, ledger row, or distribution row. Excludes pseudo-mike
       (orphan fills are reported by `orphan_fills_exist` separately).
    """
    async def q(conn: asyncpg.Connection) -> set[str]:
        seen: set[str] = set()
        # 1. Active or recent convictions
        conv_rows = await conn.fetch(
            """SELECT DISTINCT agent_name FROM agent_conviction
               WHERE symbol = $1 AND
                     (expires_at > NOW() - ($2 || ' days')::interval)""",
            symbol.upper(), str(since_days),
        )
        seen.update(r["agent_name"] for r in conv_rows)
        # 2. Ledger rows for fills on this symbol in window
        ledger_rows = await conn.fetch(
            """SELECT DISTINCT agent_name FROM agent_ledger
               WHERE symbol = $1
                 AND booked_at >= NOW() - ($2 || ' days')::interval""",
            symbol.upper(), str(since_days),
        )
        seen.update(r["agent_name"] for r in ledger_rows)
        # 3. Distribution rows
        dist_rows = await conn.fetch(
            """SELECT DISTINCT agent_name FROM agent_forecast
               WHERE symbol = $1 AND distribution IS NOT NULL
                 AND submitted_at >= NOW() - ($2 || ' days')::interval""",
            symbol.upper(), str(since_days),
        )
        seen.update(r["agent_name"] for r in dist_rows)
        return seen
    seen = await _with_conn(q)
    return sorted(seen)


def agents_with_signal_on_symbol(
    symbol: str, since_days: int = 14,
) -> list[str]:
    return asyncio.run(_agents_with_signal_on_symbol(symbol, since_days))


async def _orphan_fills_exist_for_symbol(
    symbol: str, since_days: int = 14,
) -> bool:
    """True if any fill on `symbol` in the window has no agent_ledger row —
    i.e. would render under the 'Mike (orphans)' pseudo-agent tab."""
    async def q(conn: asyncpg.Connection) -> bool:
        row = await conn.fetchrow(
            """SELECT 1 FROM fills f
               WHERE f.symbol = $1
                 AND f.filled_at::timestamptz >= NOW() - ($2 || ' days')::interval
                 AND NOT EXISTS (
                   SELECT 1 FROM agent_ledger l WHERE l.fill_id = f.id
                 )
               LIMIT 1""",
            symbol.upper(), str(since_days),
        )
        return row is not None
    return await _with_conn(q)


def orphan_fills_exist_for_symbol(symbol: str, since_days: int = 14) -> bool:
    return asyncio.run(_orphan_fills_exist_for_symbol(symbol, since_days))


async def _recent_distributions_for_symbol_agent(
    symbol: str, agent_name: str, since_days: int = 14, limit: int = 500,
) -> list[dict[str, Any]]:
    """Distribution rows (active + resolved + expired) for one (symbol, agent)
    in the window. Used by the per-(ticker, agent) panel to draw prediction
    bands. Caller is responsible for unpacking the JSONB."""
    async def q(conn: asyncpg.Connection) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            """SELECT submitted_at, expires_at, horizon,
                      time_to_target_days, distribution,
                      realized_return_pct, resolved_at
               FROM agent_forecast
               WHERE symbol = $1 AND agent_name = $2
                 AND distribution IS NOT NULL
                 AND submitted_at >= NOW() - ($3 || ' days')::interval
               ORDER BY submitted_at DESC
               LIMIT $4""",
            symbol.upper(), agent_name, str(since_days), int(limit),
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            dist = d["distribution"]
            if isinstance(dist, str):
                try:
                    d["distribution"] = json.loads(dist)
                except json.JSONDecodeError:
                    continue
            out.append(d)
        return out
    return await _with_conn(q)


def recent_distributions_for_symbol_agent(
    symbol: str, agent_name: str, since_days: int = 14, limit: int = 500,
) -> list[dict[str, Any]]:
    return asyncio.run(
        _recent_distributions_for_symbol_agent(
            symbol, agent_name, since_days, limit,
        )
    )


async def _fills_attributable_to_agent(
    symbol: str, agent_name: str | None, since_hours: int = 24 * 365,
) -> list[dict[str, Any]]:
    """Fills on `symbol` filtered by which agent the ledger attributes them to.

    Pass `agent_name=None` to get *orphan* fills (no agent_ledger row exists
    for the fill_id). Otherwise returns only fills with at least one
    agent_ledger row matching `agent_name`."""
    async def q(conn: asyncpg.Connection) -> list[dict[str, Any]]:
        if agent_name is None:
            rows = await conn.fetch(
                """SELECT f.id, f.filled_at, f.action, f.quantity,
                          f.fill_price, f.order_id, f.symbol
                   FROM fills f
                   WHERE f.symbol = $1
                     AND f.filled_at::timestamptz >= NOW() - ($2 || ' hours')::interval
                     AND NOT EXISTS (
                       SELECT 1 FROM agent_ledger l WHERE l.fill_id = f.id
                     )
                   ORDER BY f.filled_at""",
                symbol.upper(), str(since_hours),
            )
        else:
            rows = await conn.fetch(
                """SELECT DISTINCT f.id, f.filled_at, f.action, f.quantity,
                          f.fill_price, f.order_id, f.symbol
                   FROM fills f
                   JOIN agent_ledger l ON l.fill_id = f.id
                   WHERE f.symbol = $1 AND l.agent_name = $2
                     AND f.filled_at::timestamptz >= NOW() - ($3 || ' hours')::interval
                   ORDER BY f.filled_at""",
                symbol.upper(), agent_name, str(since_hours),
            )
        return [dict(r) for r in rows]
    return await _with_conn(q)


def fills_attributable_to_agent(
    symbol: str, agent_name: str | None, since_hours: int = 24 * 365,
) -> list[dict[str, Any]]:
    return asyncio.run(
        _fills_attributable_to_agent(symbol, agent_name, since_hours)
    )
