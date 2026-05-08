"""Read-side queries for the obs dashboard.

The proxy writes audit_log + tool_calls; this module shapes that data for the
Streamlit views. asyncpg is the underlying driver, but Streamlit runs sync
code, so each entry point wraps an asyncio.run().
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import asyncpg

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


def _pg_dsn() -> str:
    # Prefer env vars, fall back to config.yaml defaults the rest of the codebase uses.
    host = os.environ.get("PG_HOST", "localhost")
    port = os.environ.get("PG_PORT", "5432")
    db = os.environ.get("PG_DATABASE", "trading")
    user = os.environ.get("PG_USER", "trading")
    pw = os.environ.get("PG_PASSWORD", "5369")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(_pg_dsn(), min_size=1, max_size=4, command_timeout=10)
    return _pool


async def _list_recent_for_agent(agent: str, limit: int = 10) -> list[dict[str, Any]]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
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
            agent,
            limit,
        )
        return [dict(r) for r in rows]


def list_recent_for_agent(agent: str, limit: int = 10) -> list[dict[str, Any]]:
    return asyncio.run(_list_recent_for_agent(agent, limit))


async def _list_recent_skill_invocations(limit: int = 50) -> list[dict[str, Any]]:
    """Latest skill invocations grouped by session_id (one row per session)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
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


def list_recent_skill_invocations(limit: int = 50) -> list[dict[str, Any]]:
    return asyncio.run(_list_recent_skill_invocations(limit))


async def _get_session_exchanges(session_id: str) -> list[dict[str, Any]]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
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


def get_session_exchanges(session_id: str) -> list[dict[str, Any]]:
    return asyncio.run(_get_session_exchanges(session_id))


async def _get_session_tool_calls(session_id: str) -> list[dict[str, Any]]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT session_id, created_at, tool_round, tool_name, tool_input, tool_output, duration_ms, error
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


def get_session_tool_calls(session_id: str) -> list[dict[str, Any]]:
    return asyncio.run(_get_session_tool_calls(session_id))


async def _list_skills_for_agent(agent: str) -> list[str]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
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


def list_skills_for_agent(agent: str) -> list[str]:
    return asyncio.run(_list_skills_for_agent(agent))


async def _list_sessions_for_skill(agent: str, skill: str, limit: int = 20) -> list[dict[str, Any]]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
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
            agent,
            skill,
            limit,
        )
        return [dict(r) for r in rows]


def list_sessions_for_skill(agent: str, skill: str, limit: int = 20) -> list[dict[str, Any]]:
    return asyncio.run(_list_sessions_for_skill(agent, skill, limit))


async def _list_known_agents() -> list[str]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT agent_name FROM audit_log ORDER BY 1"
        )
        return [r["agent_name"] for r in rows]


def list_known_agents() -> list[str]:
    return asyncio.run(_list_known_agents())
