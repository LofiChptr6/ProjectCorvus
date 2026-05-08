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
