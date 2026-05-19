"""Streamlit dashboard for the local-LLM agent fleet.

Four tabs:
  1. Live grid       — agent cards with status + recent runs
  2. Skill detail    — full conversation viewer for one session, with live tile
                       embedded if the session is still running
  3. Diff (A vs B)   — side-by-side comparison of two runs of the same skill
  4. Live trace      — top-20 exposures with 5-min OHLC charts + fill markers.
                       Reads only from Postgres (positions_anchor, local_bars,
                       fills, nav_log) — never reaches into Massive directly.

Reads from Postgres (audit_log + tool_calls populated by obs/proxy.py;
positions_anchor + fills by mike's allocator; local_bars by the streamer).

Run:
    .venv/bin/streamlit run obs/dashboard.py --server.address 127.0.0.1 --server.port 8501
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Streamlit runs this file as `streamlit run obs/dashboard.py`, which puts
# obs/ on sys.path[0] but NOT the repo root — so `from obs import queries`
# fails out of the box. Resolve repo root and prepend.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx
import streamlit as st
import streamlit.components.v1 as components
from streamlit_autorefresh import st_autorefresh

from obs import queries

# ── Setup ─────────────────────────────────────────────────────────────────────

PROXY_URL = os.environ.get("LLM_PROXY_URL", "http://localhost:8001").rstrip("/")
TEMPLATE_PATH = Path(__file__).parent / "templates" / "live_tile.html"

AGENTS_ORDER = [
    "atlas", "fab", "fabless", "iron", "maya", "rex",
    "trump", "vera", "volt", "energy", "commodity",
    "mike", "cassidy", "desk",
]
AGENT_TAGLINES = {
    "atlas":     "macro · indices · rates · FX",
    "fab":       "semis · fabs · equipment",
    "fabless":   "semis · designers + ETFs",
    "iron":      "industrials · transports · defense",
    "maya":      "financials · banks",
    "rex":       "mega-cap tech ex-semi",
    "trump":     "consumer staples + discretionary",
    "vera":      "healthcare · biotech · pharma",
    "volt":      "utilities · REITs · infra",
    "energy":    "oil · gas · refiners",
    "commodity": "metals · ag · broad commod",
    "mike":      "allocator · director",
    "cassidy":   "overnight risk reviewer",
    "desk":      "hourly heartbeat",
}

st.set_page_config(
    page_title="Trading desk · live agents",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ── Cached helpers ────────────────────────────────────────────────────────────

@st.cache_data(ttl=2)
def _live_snapshot() -> list[dict[str, Any]]:
    try:
        r = httpx.get(f"{PROXY_URL}/live", timeout=2.0)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []


@st.cache_data(ttl=2)
def _recent_for_agent(agent: str, limit: int = 4) -> list[dict[str, Any]]:
    try:
        return queries.list_recent_for_agent(agent, limit=limit)
    except Exception as e:
        return [{"error": str(e)}]


@st.cache_data(ttl=4)
def _recent_invocations(limit: int = 50) -> list[dict[str, Any]]:
    try:
        return queries.list_recent_skill_invocations(limit)
    except Exception:
        return []


@st.cache_data(ttl=4)
def _session_detail(session_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        ex = queries.get_session_exchanges(session_id)
        tc = queries.get_session_tool_calls(session_id)
    except Exception:
        ex, tc = [], []
    return ex, tc


@st.cache_data(ttl=10)
def _skills_for_agent(agent: str) -> list[str]:
    try:
        return queries.list_skills_for_agent(agent)
    except Exception:
        return []


@st.cache_data(ttl=10)
def _sessions_for_skill(agent: str, skill: str, limit: int = 20) -> list[dict[str, Any]]:
    try:
        return queries.list_sessions_for_skill(agent, skill, limit=limit)
    except Exception:
        return []


# ── Live trace tab — data loaders ────────────────────────────────────────────

@st.cache_data(ttl=60)
def _live_total_assets() -> dict[str, Any]:
    """Latest nav_log row. Falls back to {} if the desk hasn't booted."""
    try:
        return queries.latest_nav() or {}
    except Exception:
        return {}


@st.cache_data(ttl=60)
def _live_nav_history_full() -> list[dict[str, Any]]:
    """Full nav_log — small table, safe to load. Picker filters in-memory."""
    try:
        return queries.nav_history(days=None)
    except Exception:
        return []


# Range options for the Robinhood-style picker. (label, days_back or sentinel).
# 'YTD' = since Jan 1 of current year; 'ALL' = no cutoff (NAV chart only —
# per-ticker chart tops out at 1Y because local_bars_daily holds ~365 days).
_NAV_RANGE_OPTIONS = ["2W", "1M", "3M", "YTD", "1Y", "ALL"]
_SYMBOL_RANGE_OPTIONS = ["2W", "1M", "3M", "YTD", "1Y"]


@st.cache_data(ttl=60)
def _live_top_exposures(limit: int = 20) -> list[dict[str, Any]]:
    """Top symbols ranked by gross dollar notional. Joins the latest
    positions_anchor (qty per symbol) with the latest local_bars close
    (mark) and returns sorted by abs(qty*mark) desc."""
    try:
        return queries.top_exposures(limit=limit)
    except Exception:
        return []


@st.cache_data(ttl=60)
def _live_daily_bars_for_symbol(symbol: str, days: int = 365) -> list[dict[str, Any]]:
    """Trailing-year daily bars for one symbol — what the per-ticker chart
    paints. Backed by local_bars_daily, refreshed by the daily-bars timer."""
    try:
        return queries.recent_daily_bars(symbol, days=days)
    except Exception:
        return []


@st.cache_data(ttl=300)
def _cached_fill_context(symbol: str, filled_at_iso: str) -> dict | None:
    """Memoized fill-context bundle (immutable once the trade lands).
    300s TTL so repeated clicks on the same fill are instant."""
    try:
        return queries.get_fill_context(symbol, filled_at_iso)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@st.cache_data(ttl=30)
def _live_ops_health() -> dict[str, Any]:
    """Queue + streamer health snapshot. Short TTL so the live trace tab
    surfaces backlogs / ingest stalls quickly."""
    try:
        return queries.ops_health()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@st.cache_data(ttl=60)
def _live_trades_for_symbol(symbol: str, since_hours: int = 24) -> list[dict[str, Any]]:
    try:
        return queries.recent_fills(symbol, since_hours=since_hours)
    except Exception:
        return []


# ── Per-(symbol × agent) live-trace caches ──────────────────────────────────


@st.cache_data(ttl=60)
def _live_agents_for_symbol(symbol: str, since_days: int = 14) -> list[str]:
    try:
        return queries.agents_with_signal_on_symbol(symbol, since_days)
    except Exception as e:
        log.warning("agents_with_signal_on_symbol(%s) failed: %s", symbol, e)
        return []


@st.cache_data(ttl=60)
def _live_orphan_fills_exist(symbol: str, since_days: int = 14) -> bool:
    try:
        return queries.orphan_fills_exist_for_symbol(symbol, since_days)
    except Exception:
        return False


@st.cache_data(ttl=60)
def _live_distributions_for_symbol_agent(
    symbol: str, agent_name: str, since_days: int = 14,
) -> list[dict[str, Any]]:
    try:
        return queries.recent_distributions_for_symbol_agent(
            symbol, agent_name, since_days=since_days,
        )
    except Exception as e:
        log.warning("recent_distributions_for_symbol_agent(%s,%s) failed: %s",
                    symbol, agent_name, e)
        return []


@st.cache_data(ttl=60)
def _live_fills_for_agent(
    symbol: str, agent_name: str | None, since_hours: int = 24 * 365,
) -> list[dict[str, Any]]:
    """agent_name=None ⇒ orphan fills (no ledger row)."""
    try:
        return queries.fills_attributable_to_agent(symbol, agent_name, since_hours)
    except Exception as e:
        log.warning("fills_attributable_to_agent(%s,%s) failed: %s",
                    symbol, agent_name, e)
        return []


# ── Formatting ────────────────────────────────────────────────────────────────


def _fmt_time(ts: Any) -> str:
    if ts is None:
        return "—"
    if isinstance(ts, dt.datetime):
        ts = ts.isoformat()
    s = str(ts)
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        delta = dt.datetime.now(dt.timezone.utc) - d.astimezone(dt.timezone.utc)
        secs = int(delta.total_seconds())
        if secs < 60: return f"{secs}s ago"
        if secs < 3600: return f"{secs // 60}m ago"
        if secs < 86400: return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return s[-19:]


def _fmt_ms(ms: Any) -> str:
    if not ms:
        return "—"
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return "—"
    if ms < 1000: return f"{ms}ms"
    if ms < 60000: return f"{ms // 1000}s"
    return f"{ms // 60000}m{(ms // 1000) % 60:02d}s"


def _fmt_tokens(n: Any) -> str:
    if not n:
        return "0"
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    if n < 1000: return str(n)
    if n < 1_000_000: return f"{n / 1000:.1f}K"
    return f"{n / 1_000_000:.1f}M"


# ── Per-agent inbox (chat) ────────────────────────────────────────────────────
# Each card has an "Ask <agent>" form. Submit posts a row to agent_inbox and
# fires `python scripts/run_skill.py <agent> respond` as a fire-and-forget
# subprocess; the autorefresh polls until response_body lands.


async def _post_question_async(agent: str, body: str, sender: str = "user") -> int:
    """Open a fresh asyncpg connection, INSERT, return id. The dashboard
    runs in synchronous Streamlit code — `post_question` wraps this."""
    import asyncpg
    conn = await asyncpg.connect(queries._pg_dsn(), command_timeout=10)
    try:
        row = await conn.fetchrow(
            """INSERT INTO agent_inbox (agent_name, body, sender)
               VALUES ($1,$2,$3) RETURNING id""",
            agent, body, sender,
        )
        return int(row["id"])
    finally:
        await conn.close()


def post_question(agent: str, body: str, sender: str = "user") -> int:
    """Sync wrapper for Streamlit. Per-call connection — see queries module
    for the why-not-pool rationale."""
    import asyncio
    return asyncio.run(_post_question_async(agent, body, sender))


def fire_respond_subprocess(agent: str) -> int:
    """Fire-and-forget; returns the subprocess pid."""
    import os as _os
    import subprocess
    p = subprocess.Popen(
        [sys.executable, str(_REPO_ROOT / "scripts/run_skill.py"), agent, "respond"],
        cwd=str(_REPO_ROOT),
        env=dict(_os.environ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return p.pid


async def _recent_qa_async(agent: str, limit: int = 3) -> list[dict[str, Any]]:
    import asyncpg
    conn = await asyncpg.connect(queries._pg_dsn(), command_timeout=10)
    try:
        rows = await conn.fetch(
            """SELECT id, body, sender, created_at, responded_at, response_body
               FROM agent_inbox WHERE agent_name=$1
               ORDER BY created_at DESC LIMIT $2""",
            agent, limit,
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


@st.cache_data(ttl=2)
def _recent_qa(agent: str, limit: int = 3) -> list[dict[str, Any]]:
    import asyncio
    try:
        return asyncio.run(_recent_qa_async(agent, limit))
    except Exception:
        return []


def _render_chat_form(agent: str) -> None:
    with st.form(key=f"ask_form_{agent}", clear_on_submit=True, border=False):
        cols = st.columns([4, 1])
        with cols[0]:
            q = st.text_input(
                "Ask", key=f"ask_input_{agent}",
                label_visibility="collapsed",
                placeholder=f"Ask {agent} a question…",
            )
        with cols[1]:
            submitted = st.form_submit_button("Send", use_container_width=True)
        if submitted and q.strip():
            try:
                inbox_id = post_question(agent, q.strip())
                fire_respond_subprocess(agent)
                st.toast(f"Sent to {agent} (inbox_id={inbox_id})", icon="📤")
            except Exception as e:
                st.error(f"Failed to send: {type(e).__name__}: {e}")
            st.rerun()


def _render_recent_qa(agent: str) -> None:
    qa = _recent_qa(agent, limit=3)
    if not qa:
        return
    with st.expander(f"Recent Q&A ({len(qa)})"):
        for row in qa:
            q_body = (row.get("body") or "")[:240]
            a_body = (row.get("response_body") or "").strip()
            sent_at = _fmt_time(row.get("created_at"))
            st.markdown(f"**Q** ({sent_at}): {q_body}")
            if a_body:
                st.markdown(f"**A:** {a_body[:600]}")
            else:
                st.caption("(pending — autorefresh will pull the response)")
            st.divider()


# ── Per-agent news feed (Phase A) ─────────────────────────────────────────────
# Reads news_items rows tagged for the given agent (or for mike/cassidy, the
# desk-wide high-importance feed). Earnings + M&A items get a ⭐ marker.

_DIRECTOR_AGENTS_DASH = {"mike", "cassidy"}


async def _recent_news_for_agent_async(agent: str, limit: int = 8) -> list[dict[str, Any]]:
    import asyncpg
    conn = await asyncpg.connect(queries._pg_dsn(), command_timeout=10)
    try:
        if agent in _DIRECTOR_AGENTS_DASH:
            # Desk-wide earnings/M&A/guidance view — no agent_tag filter.
            rows = await conn.fetch(
                """SELECT id, symbol, headline, url, sentiment, category, importance,
                          published_at, fetched_at, provider, agent_tags
                   FROM news_items
                   WHERE importance = 'high'
                     AND COALESCE(published_at, fetched_at::timestamptz)
                         > NOW() - INTERVAL '6 hours'
                   ORDER BY COALESCE(published_at, fetched_at::timestamptz) DESC
                   LIMIT $1""",
                limit,
            )
        else:
            rows = await conn.fetch(
                """SELECT id, symbol, headline, url, sentiment, category, importance,
                          published_at, fetched_at, provider, agent_tags
                   FROM news_items
                   WHERE agent_tags @> ARRAY[$1]::text[]
                     AND COALESCE(published_at, fetched_at::timestamptz)
                         > NOW() - INTERVAL '4 hours'
                   ORDER BY (importance = 'high') DESC,
                            COALESCE(published_at, fetched_at::timestamptz) DESC
                   LIMIT $2""",
                agent, limit,
            )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


@st.cache_data(ttl=10)
def _recent_news_for_agent(agent: str, limit: int = 8) -> list[dict[str, Any]]:
    """10-second cache — matches the 10-min ingest cadence (no point hitting PG
    every 2s from the autorefresh)."""
    import asyncio
    try:
        return asyncio.run(_recent_news_for_agent_async(agent, limit))
    except Exception:
        return []


def _render_news_expander(agent: str) -> None:
    rows = _recent_news_for_agent(agent, limit=8)
    if not rows:
        return
    high_count = sum(1 for r in rows if (r.get("importance") or "") == "high")
    label = f"News ({len(rows)})"
    if high_count:
        label = f"News ({len(rows)}, ⭐{high_count} earnings/M&A)"
    with st.expander(label):
        for r in rows:
            sym = r.get("symbol") or "—"
            head = (r.get("headline") or "")[:180]
            cat = str(r.get("category") or "general").replace("_", " ")
            sent = str(r.get("sentiment") or "").lower()
            pub = _fmt_time(r.get("published_at") or r.get("fetched_at"))
            url = r.get("url")
            star = "⭐ " if (r.get("importance") or "") == "high" else ""
            sent_dot = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}.get(sent, "")
            st.markdown(f"{star}**{sym}** · {cat} · {pub} {sent_dot}")
            if url:
                st.markdown(f"[{head}]({url})")
            else:
                st.markdown(head)
            st.divider()


# ── Tab 1: Live grid ──────────────────────────────────────────────────────────


async def _q_expiring_convictions(within_min: int) -> list[dict[str, Any]]:
    import asyncpg
    conn = await asyncpg.connect(queries._pg_dsn(), command_timeout=5)
    try:
        rows = await conn.fetch(
            """SELECT agent_name, symbol, direction, conviction, expires_at,
                      EXTRACT(EPOCH FROM (expires_at - NOW())) AS seconds_left
               FROM agent_conviction
               WHERE expires_at > NOW()
                 AND expires_at < NOW() + ($1 || ' minutes')::interval
                 AND conviction > 0
               ORDER BY expires_at ASC
               LIMIT 30""",
            str(int(within_min)),
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


@st.cache_data(ttl=10)
def _expiring_convictions(within_min: int = 15) -> list[dict[str, Any]]:
    """Convictions whose expires_at is within `within_min` minutes from now."""
    try:
        return asyncio.run(_q_expiring_convictions(within_min))
    except Exception:
        return []


async def _q_recent_ocap_rebalances(hours: int) -> list[dict[str, Any]]:
    import asyncpg
    conn = await asyncpg.connect(queries._pg_dsn(), command_timeout=5)
    try:
        rows = await conn.fetch(
            """SELECT id, status, enqueued_at, finished_at,
                      payload->>'source_agent' AS source_agent,
                      payload->>'source_symbol' AS source_symbol,
                      payload->>'convictions_materially_changed' AS n_material,
                      error
               FROM agent_job
               WHERE job_type = 'ocap_rebalance'
                 AND enqueued_at > NOW() - ($1 || ' hours')::interval
               ORDER BY enqueued_at DESC
               LIMIT 50""",
            str(int(hours)),
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


@st.cache_data(ttl=10)
def _recent_ocap_rebalances(hours: int = 24) -> list[dict[str, Any]]:
    """OCAP-fired rebalance jobs from the last N hours with final status."""
    try:
        return asyncio.run(_q_recent_ocap_rebalances(hours))
    except Exception:
        return []


async def _q_muted_agents() -> list[str]:
    import asyncpg
    conn = await asyncpg.connect(queries._pg_dsn(), command_timeout=5)
    try:
        rows = await conn.fetch(
            """SELECT DISTINCT ks.agent_name
               FROM kill_switch ks
               WHERE ks.is_active = 1
                 AND ks.agent_name IS NOT NULL
                 AND ks.id = (
                     SELECT MAX(id) FROM kill_switch
                     WHERE agent_name = ks.agent_name
                 )
               ORDER BY ks.agent_name"""
        )
        return [r["agent_name"] for r in rows]
    finally:
        await conn.close()


@st.cache_data(ttl=10)
def _muted_agents() -> list[str]:
    """Agents whose per-agent kill is active (their convictions get filtered
    out of the allocator's vote tally)."""
    try:
        return asyncio.run(_q_muted_agents())
    except Exception:
        return []


def _render_desk_status_strip() -> None:
    """Three small panels showing things the per-agent grid doesn't surface:
    convictions about to expire, recent OCAP-fired rebalances, and muted
    agents. Compact — each in an expander, collapsed by default."""
    expiring = _expiring_convictions(within_min=15)
    rebalances = _recent_ocap_rebalances(hours=24)
    muted = _muted_agents()

    cols = st.columns(3)
    with cols[0]:
        label = f"⏰ Expiring soon ({len(expiring)})"
        with st.expander(label, expanded=False):
            if not expiring:
                st.caption("no convictions expiring in the next 15 minutes.")
            else:
                for r in expiring[:10]:
                    mins = max(0, int((r.get("seconds_left") or 0) // 60))
                    st.markdown(
                        f"- **{r['agent_name']}** · {r['symbol']} · "
                        f"{r['direction']} · conv {float(r['conviction']):.2f} · "
                        f"**{mins}m**"
                    )
    with cols[1]:
        n_ok = sum(1 for r in rebalances if r["status"] == "done")
        n_err = sum(1 for r in rebalances if r["status"] == "failed")
        label = f"🔀 OCAP rebalances 24h ({len(rebalances)} · {n_ok} ok / {n_err} err)"
        with st.expander(label, expanded=False):
            if not rebalances:
                st.caption("no OCAP-fired rebalances in the last 24h.")
            else:
                for r in rebalances[:15]:
                    src = r.get("source_agent") or "?"
                    sym = r.get("source_symbol") or "?"
                    icon = {"done": "✓", "failed": "✗", "skipped": "·"}.get(r["status"], "?")
                    enq = _fmt_time(r.get("enqueued_at"))
                    st.markdown(
                        f"- {icon} {enq} · {src}/{sym} "
                        f"(n_material={r.get('n_material') or '?'})"
                    )
    with cols[2]:
        label = f"🔇 Muted agents ({len(muted)})"
        with st.expander(label, expanded=False):
            if not muted:
                st.caption("no per-agent kill switches active — every agent's votes count.")
            else:
                st.caption("These agents still analyze + publish convictions, but mike "
                           "filters their votes at the allocator load step:")
                for a in muted:
                    st.markdown(f"- **{a}**")


def render_live_grid() -> None:
    st_autorefresh(interval=2000, key="grid_autorefresh")
    live = {s["agent"]: s for s in _live_snapshot()}

    st.caption(
        f"Last refresh: {dt.datetime.now().strftime('%H:%M:%S')}  ·  "
        f"Live sessions: {len(live)}  ·  Proxy: {PROXY_URL}"
    )

    _render_desk_status_strip()

    cols = st.columns(3)
    for i, agent in enumerate(AGENTS_ORDER):
        with cols[i % 3]:
            recent = _recent_for_agent(agent, limit=4)
            is_running = agent in live
            had_error = bool(recent and not is_running and recent[0].get("error"))

            dot = "🟢" if is_running else ("🔴" if had_error else "⚪")
            tagline = AGENT_TAGLINES.get(agent, "")
            with st.container(border=True):
                st.markdown(f"### {dot} {agent}")
                st.caption(tagline)

                if is_running:
                    info = live[agent]
                    started = info.get("started_at") or 0
                    elapsed = int(time.time() - started)
                    st.markdown(
                        f"**▶ /{info.get('skill','?')}** &nbsp;"
                        f"`{_fmt_tokens(info.get('tokens_so_far',0))} tok · {elapsed}s`"
                    )
                    preview = (info.get("preview") or "").strip()
                    if preview:
                        st.code(preview[-180:], language="text")
                    if st.button("Open live session", key=f"open_{agent}"):
                        st.query_params.clear()
                        st.query_params["view"] = "detail"
                        st.query_params["session"] = info["session_id"]
                        st.rerun()

                if recent:
                    for r in recent[: 3 if not is_running else 2]:
                        sid = r.get("session_id")
                        if not sid:
                            continue
                        skill = r.get("skill_name") or r.get("routine") or "?"
                        line = (
                            f"○ /{skill} · {_fmt_time(r.get('created_at'))} · "
                            f"{_fmt_ms(r.get('duration_ms'))} · "
                            f"{_fmt_tokens((r.get('prompt_tokens') or 0) + (r.get('completion_tokens') or 0))} tok"
                        )
                        if r.get("error"):
                            line = "⚠ " + line
                        if st.button(line, key=f"recent_{agent}_{sid}_{r.get('request_index',0)}",
                                     use_container_width=True):
                            st.query_params.clear()
                            st.query_params["view"] = "detail"
                            st.query_params["session"] = sid
                            st.rerun()
                else:
                    st.caption("(no recorded runs yet)")

                # Per-agent inbox: ask + recent Q&A (omit for `desk` — heartbeat-only).
                if agent != "desk":
                    _render_chat_form(agent)
                    _render_recent_qa(agent)
                    _render_news_expander(agent)


# ── Tab 2: Skill detail ───────────────────────────────────────────────────────


def render_skill_detail() -> None:
    session_id = st.query_params.get("session", "")
    if not session_id:
        recent = _recent_invocations(limit=50)
        if not recent:
            st.info("No sessions logged yet. Run a skill (e.g. atlas-review --dev) to populate.")
            return
        labels = []
        sid_by_label = {}
        for r in recent:
            label = (
                f"/{r.get('skill_name') or r.get('routine') or '?'} · {r.get('agent_name')} · "
                f"{_fmt_time(r.get('started_at'))} · {_fmt_ms(r.get('duration_ms'))} · "
                f"{_fmt_tokens((r.get('prompt_tokens') or 0) + (r.get('completion_tokens') or 0))} tok"
                + ("  ⚠" if r.get("had_error") else "")
            )
            labels.append(label)
            sid_by_label[label] = r["session_id"]
        chosen = st.selectbox("Pick a session", labels, key="detail_picker")
        if chosen:
            session_id = sid_by_label[chosen]
            st.query_params["session"] = session_id

    if not session_id:
        return

    exchanges, tool_calls = _session_detail(session_id)
    if not exchanges:
        st.warning(f"Session `{session_id}` not found in audit_log yet (still running?).")
    else:
        first = exchanges[0]
        last = exchanges[-1]
        st.markdown(f"### `{session_id}`")
        st.caption(
            f"agent **{first.get('agent_name')}** · skill **{first.get('skill_name') or first.get('routine')}** · "
            f"{len(exchanges)} exchanges · {sum(e.get('tool_rounds') or 0 for e in exchanges)} tool rounds · "
            f"{_fmt_tokens(sum((e.get('prompt_tokens') or 0) for e in exchanges))} prompt + "
            f"{_fmt_tokens(sum((e.get('completion_tokens') or 0) for e in exchanges))} completion · "
            f"started {first.get('created_at')} · finished `{last.get('finish_reason')}`"
        )

    # Live tile if session is still running
    live = {s["session_id"]: s for s in _live_snapshot()}
    if session_id in live:
        info = live[session_id]
        st.markdown("#### 🔴 LIVE")
        html = TEMPLATE_PATH.read_text()
        html = (html
                .replace("{SESSION_ID}", session_id)
                .replace("{PROXY_URL}", PROXY_URL)
                .replace("{AGENT}", info.get("agent", "?"))
                .replace("{SKILL}", info.get("skill", "?")))
        components.html(html, height=560, scrolling=False)

    if not exchanges:
        return

    st.markdown("#### Conversation")
    if first.get("system_prompt"):
        with st.expander("📜 system prompt", expanded=False):
            st.code(first["system_prompt"], language="markdown")

    for ex in exchanges:
        idx = ex.get("request_index", 0)
        st.markdown(f"##### turn {idx + 1} · `{_fmt_ms(ex.get('duration_ms'))}` · "
                    f"`{_fmt_tokens(ex.get('prompt_tokens'))}` in / "
                    f"`{_fmt_tokens(ex.get('completion_tokens'))}` out · "
                    f"finish_reason=`{ex.get('finish_reason')}`")

        # Show only the LATEST role-assistant turn from this exchange's messages,
        # plus the user/tool-result that triggered it.
        msgs = ex.get("messages_parsed", [])
        # Find the trailing assistant message (the one this exchange produced).
        # Everything before it is "input"; everything from it onwards is "output".
        boundary = len(msgs) - 1
        while boundary >= 0 and msgs[boundary].get("role") != "assistant":
            boundary -= 1
        if idx == 0 and boundary > 0:
            # First exchange: render the leading user message inline
            for m in msgs[:boundary]:
                _render_message(m)
        else:
            # Subsequent exchanges: only show the most recent user/tool message before the assistant turn
            tail_user = None
            for m in reversed(msgs[:boundary]):
                if m.get("role") == "user":
                    tail_user = m
                    break
            if tail_user:
                _render_message(tail_user, label_prefix="↩ tool results from previous turn")

        # The assistant turn
        if boundary >= 0:
            _render_message(msgs[boundary])

        if ex.get("thinking_block"):
            with st.expander(f"🧠 thinking ({len(ex['thinking_block'])} chars)", expanded=False):
                st.markdown(f"<div style='color:#888;font-style:italic;white-space:pre-wrap'>"
                            f"{ex['thinking_block']}</div>", unsafe_allow_html=True)

        if ex.get("error"):
            st.error(ex["error"])

    if tool_calls:
        with st.expander(f"🔧 tool calls ({len(tool_calls)})", expanded=False):
            for tc in tool_calls:
                st.markdown(f"**{tc.get('tool_name')}** · round {tc.get('tool_round')}")
                st.json(tc.get("tool_input_parsed", {}))


def _render_message(m: dict[str, Any], label_prefix: str = "") -> None:
    role = m.get("role", "?")
    content = m.get("content", "")
    label = label_prefix or {"user": "user", "assistant": "assistant", "tool": "tool result"}.get(role, role)
    color = {"user": "#1e3a8a", "assistant": "#065f46", "tool": "#7c2d12"}.get(role, "#374151")

    st.markdown(f"<div style='padding:4px 0;color:{color};font-weight:600'>· {label}</div>",
                unsafe_allow_html=True)

    if isinstance(content, str):
        st.markdown(f"<div style='padding-left:1em;white-space:pre-wrap;font-size:13px'>"
                    f"{_html_escape(content)[:6000]}</div>", unsafe_allow_html=True)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                txt = block.get("text", "")
                # Strip <think>…</think> for clean display (we render thinking separately)
                import re
                clean = re.sub(r"<think>.*?</think>", "", txt, flags=re.DOTALL).strip()
                st.markdown(f"<div style='padding-left:1em;white-space:pre-wrap;font-size:13px'>"
                            f"{_html_escape(clean)[:6000]}</div>", unsafe_allow_html=True)
            elif btype == "tool_use":
                st.markdown(f"<div style='padding-left:1em;color:#f59e0b'>→ tool_use: "
                            f"<b>{_html_escape(block.get('name','?'))}</b></div>",
                            unsafe_allow_html=True)
                st.code(json.dumps(block.get("input", {}), indent=2)[:2000], language="json")
            elif btype == "tool_result":
                content_inner = block.get("content", "")
                if isinstance(content_inner, list):
                    content_inner = "\n".join(str(b.get("text", "")) for b in content_inner if isinstance(b, dict))
                st.markdown(f"<div style='padding-left:1em;color:#a16207'>← tool_result</div>",
                            unsafe_allow_html=True)
                st.code(str(content_inner)[:2000], language="text")


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ── Main ──────────────────────────────────────────────────────────────────────


def render_live_trace() -> None:
    """Desk NAV chart + top-20 exposures with 5-min OHLC charts and fill
    markers. Polls every 5 min via st_autorefresh. Reads exclusively from
    Postgres (positions_anchor, local_bars, fills, nav_log) — never reaches
    into Massive directly. The bar streamer (scripts/stream_bars.py) keeps
    local_bars fresh."""
    st_autorefresh(interval=300000, key="live_trace_autorefresh")

    nav = _live_total_assets()
    if nav:
        cols = st.columns(3)
        cols[0].metric("Desk NAV",
                       f"${float(nav.get('desk_nav') or 0):,.0f}")
        cols[1].metric("Cash",
                       f"${float(nav.get('cash_balance') or 0):,.0f}")
        recorded = nav.get("recorded_at")
        cols[2].metric("NAV as of", _fmt_time(recorded) if recorded else "—")
    else:
        st.info("No NAV anchor yet — waiting for mike's first rebalance.")

    nav_hist_full = _live_nav_history_full()
    if nav_hist_full:
        _render_nav_chart(nav_hist_full)
        st.divider()

    _render_ops_health()
    st.divider()

    st.subheader("Top exposures (live)")
    top = _live_top_exposures(limit=20)
    if not top:
        st.info("No open positions to display.")
        return

    # 3:1 split — charts on the left, persistent context panel on the right.
    # Clicking a fill triangle updates st.session_state["live_trace_selected_fill"]
    # and the right panel swaps to the new fill's full context (contributing
    # agents, conviction, theses, session, tool calls).
    main_col, panel_col = st.columns([3, 1])

    with main_col:
        for row in top:
            sym = row["symbol"]
            with st.container():
                head_cols = st.columns([1, 1, 1, 1])
                head_cols[0].markdown(f"### {sym}")
                head_cols[1].metric("Qty", f"{row['qty']:,.0f}")
                head_cols[2].metric("Mark", f"${(row['mark'] or 0):,.2f}")
                head_cols[3].metric("Exposure", f"${row['exposure']:,.0f}")

                # Load the trailing year of daily bars for this symbol; the
                # per-chart range picker filters in-memory across 2W → 1Y
                # without re-querying. Shared across all per-agent panels
                # under this ticker — one query per ticker, not per panel.
                bars = _live_daily_bars_for_symbol(sym, days=365)
                if not bars:
                    st.caption(f"no daily bars cached for {sym}")
                    st.divider()
                    continue

                _render_symbol_agent_tabs(sym, bars)
            st.divider()

    with panel_col:
        st.markdown("### Trade context")
        sel = st.session_state.get("live_trace_selected_fill")
        if sel:
            _render_fill_context_panel(sel)
        else:
            st.info(
                "Click any fill triangle on the left to see the agent's "
                "full reasoning — conviction rationale, news browsed, "
                "quant model outputs, and the LLM session thinking + "
                "final response that produced the trade."
            )


def _render_symbol_agent_tabs(symbol: str, bars: list[dict[str, Any]]) -> None:
    """Render one chart per (symbol × agent) under the symbol header.

    Each tab contains a chart for the same symbol but filtered to a single
    agent's perspective: fills attributable to that agent only, plus that
    agent's prediction bands (single neutral color, opacity ∝ p).

    A "Mike (orphans)" pseudo-agent tab surfaces fills with no agent_ledger
    attribution (no prediction bands for that tab — Mike doesn't emit
    distributions himself).

    The tab list is filtered to agents who actually have signal on this
    symbol within the last 14 days — otherwise every ticker would render
    the full 10-agent roster (visual noise)."""
    agents = _live_agents_for_symbol(symbol, since_days=14)
    has_orphans = _live_orphan_fills_exist(symbol, since_days=14)

    if not agents and not has_orphans:
        st.caption(f"no agent has signal on {symbol} in the last 14d.")
        return

    tab_labels = list(agents)
    if has_orphans:
        tab_labels.append("Mike (orphans)")

    tabs = st.tabs(tab_labels)
    for tab, label in zip(tabs, tab_labels):
        with tab:
            if label == "Mike (orphans)":
                fills = _live_fills_for_agent(symbol, agent_name=None)
                _render_symbol_chart(
                    symbol, bars, fills,
                    distributions=None,
                    chart_key_suffix="__orphan",
                )
            else:
                agent = label
                fills = _live_fills_for_agent(symbol, agent_name=agent)
                dists = _live_distributions_for_symbol_agent(symbol, agent,
                                                              since_days=14)
                _render_symbol_chart(
                    symbol, bars, fills,
                    distributions=dists,
                    chart_key_suffix=f"__{agent}",
                )


def _interactive_x():
    """Wheel-zoom + drag-pan bound to the X scale only. Y stays anchored so
    price moves don't disappear off-screen when the user scans the time axis."""
    import altair as alt
    return alt.selection_interval(bind="scales", encodings=["x"])


def _filter_by_window(rows: list[dict[str, Any]], window: str,
                      time_key: str = "recorded_at") -> list[dict[str, Any]]:
    """Slice a chronologically-ordered list of rows down to one of the
    Robinhood-style windows. Caller passes the timestamp column name
    (bar_time for 5-min bars, bar_date for daily bars, filled_at for fills).
    Handles both `datetime` and `date` time keys."""
    if not rows or window == "ALL":
        return rows
    from datetime import date, datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    if window == "YTD":
        cutoff_dt = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    else:
        days_map = {"2W": 14, "1M": 30, "3M": 90, "1Y": 365}
        d = days_map.get(window)
        if d is None:
            return rows
        cutoff_dt = now - timedelta(days=d)
    cutoff_date = cutoff_dt.date()

    def _ge(v) -> bool:
        if isinstance(v, datetime):
            return v >= cutoff_dt
        if isinstance(v, date):
            return v >= cutoff_date
        return True
    return [r for r in rows if _ge(r[time_key])]


def _fmt_age(seconds: float | None) -> str:
    """Human-friendly age — '12s', '4.2m', '3.1h', '2.4d'."""
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _render_ops_health() -> None:
    """Compact ops-health strip: queue depth + worker count + data freshness.
    Lets the operator see ingest stalls or backlog at a glance."""
    h = _live_ops_health()
    if h.get("error"):
        st.warning(f"ops health: {h['error']}")
        return
    q = h.get("queue", {})
    lb = h.get("local_bars", {})
    lbd = h.get("local_bars_daily", {})

    cols = st.columns(6)
    queued = q.get("by_status", {}).get("queued", 0)
    running = q.get("by_status", {}).get("running", 0)
    failed_1h = q.get("failed_1h", 0)
    cols[0].metric("Queued", queued,
                   help="Jobs waiting in agent_job (status='queued').")
    cols[1].metric("Running", running,
                   help="Jobs currently in flight by a queue worker.")
    cols[2].metric("Workers", q.get("active_workers", 0),
                   help="Distinct worker_ids with at least one running job.")
    cols[3].metric("Failed (1h)", failed_1h,
                   delta=("error" if failed_1h > 0 else None),
                   delta_color="inverse")
    cols[4].metric("5-min lag", _fmt_age(lb.get("lag_s")),
                   help=f"Newest ingest into local_bars "
                        f"({lb.get('rows', 0):,} rows, {lb.get('symbols', 0)} symbols)")
    cols[5].metric("Daily lag", _fmt_age(lbd.get("lag_s")),
                   help=f"Newest ingest into local_bars_daily "
                        f"({lbd.get('rows', 0):,} rows, {lbd.get('symbols', 0)} symbols)")

    oldest = q.get("oldest_queued_age_s")
    if oldest and oldest > 600:
        st.warning(f"Oldest queued job has been waiting {_fmt_age(oldest)} — "
                   f"workers may be saturated or wedged.")


def _render_nav_chart(nav_hist_full: list[dict[str, Any]]) -> None:
    """Desk NAV time series with Robinhood-style range picker + % return
    over the displayed window. X-axis wheel-zoom / drag-pan; Y anchored.
    Shares the picker, label, and zoom helpers with the per-symbol charts
    so the visual idiom is identical top-to-bottom."""
    window = _range_picker(key="nav_range", default="1M")
    rows = _filter_by_window(nav_hist_full, window, time_key="recorded_at")
    if len(rows) < 2:
        st.info(f"Not enough NAV history in the {window} window "
                f"({len(rows)} snapshot(s)).")
        return

    _render_pct_label(rows, time_key="recorded_at",
                      value_key="desk_nav", count_suffix="snapshots")

    import pandas as pd
    df = pd.DataFrame([{
        "recorded_at": r["recorded_at"],
        "desk_nav": float(r["desk_nav"]),
        "cash_balance": float(r["cash_balance"]),
    } for r in rows])
    try:
        import altair as alt
        line = alt.Chart(df).mark_line(color="#1f77b4").encode(
            x=alt.X("recorded_at:T", title=None),
            y=alt.Y("desk_nav:Q", title="Desk NAV ($)",
                    scale=alt.Scale(zero=False)),
            tooltip=["recorded_at:T", "desk_nav:Q", "cash_balance:Q"],
        ).properties(height=260)
        st.altair_chart(line.add_params(_interactive_x()),
                        use_container_width=True)
    except ImportError:
        st.line_chart(df.set_index("recorded_at")["desk_nav"], height=260)


def _fmt_money_compact(amount: float) -> str:
    """Tight notional label for chart annotations: $812, $5.4K, $1.2M."""
    if amount < 1000:
        return f"${amount:,.0f}"
    if amount < 1_000_000:
        return f"${amount / 1000:.1f}K"
    return f"${amount / 1_000_000:.2f}M"


def _format_span(seconds: float) -> str:
    """Compact span label: '2.3h', '47m', '3.1d'. Used in the %return-over-
    window pairing under both NAV and per-symbol charts."""
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 60 * 90:
        return f"{seconds / 60:.0f}m"
    if seconds < 3600 * 36:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _render_pct_label(rows: list[dict[str, Any]], time_key: str,
                      value_key: str, count_suffix: str = "snapshots") -> None:
    """Shared '▲ +0.42% over 2 weeks (N items)' label, colored green/red."""
    if len(rows) < 2:
        return
    first = float(rows[0][value_key]) or 0.0
    last = float(rows[-1][value_key]) or 0.0
    if not first:
        return
    pct = (last / first - 1.0) * 100.0
    span = (rows[-1][time_key] - rows[0][time_key]).total_seconds()
    arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "·")
    color = "#2ca02c" if pct > 0 else ("#d62728" if pct < 0 else "#888")
    st.markdown(
        f"<div style='font-size:1.0rem; margin-top:-0.25rem; margin-bottom:0.25rem;'>"
        f"<span style='color:{color}; font-weight:600;'>{arrow} {pct:+.2f}%</span>"
        f" &nbsp;<span style='color:#888;'>over {_format_span(span)} "
        f"({len(rows)} {count_suffix})</span></div>",
        unsafe_allow_html=True,
    )


def _range_picker(key: str, default: str = "2W",
                  options: list[str] | None = None) -> str:
    """Robinhood-style segmented control. Falls back to a horizontal radio
    on Streamlit versions before segmented_control landed."""
    opts = options or _NAV_RANGE_OPTIONS
    if default not in opts:
        default = opts[0]
    try:
        sel = st.segmented_control(
            "Range", opts, default=default,
            key=key, label_visibility="collapsed",
        )
    except AttributeError:
        sel = st.radio(
            "Range", opts, index=opts.index(default),
            horizontal=True, key=key, label_visibility="collapsed",
        )
    return sel or default


_BAND_ALPHA_FLOOR = 0.03
_BAND_ALPHA_CAP = 0.6
_BAND_ROW_CAP = 500


def _distribution_band_rows(
    distributions: list[dict[str, Any]],
    y_clip_lo: float, y_clip_hi: float,
) -> list[dict[str, Any]]:
    """Convert a list of agent_forecast rows (each carrying anchor_price +
    bins) into per-rectangle band rows for Altair's `mark_rect`.

    Edge math (per user spec):
      - y_center_i = anchor_price · (1 + x_i / 100)         (axis=return_pct)
      - Inner bins: y_lo/y_hi = midpoint between adjacent y_centers
      - Outer bins extend to ±∞ → clipped to [y_clip_lo, y_clip_hi]
      - t_lo = submitted_at; t_hi = submitted_at + horizon_minutes
      - alpha = clip(p, FLOOR, CAP) so tiny-p still registers, dominant-p
        doesn't drown the price line

    Hard cap on output rows (`_BAND_ROW_CAP`) to protect chart-render perf
    on OCAP storms. Newest forecasts are kept first.
    """
    from meta_agent.distribution_validator import horizon_to_minutes
    import datetime as _dt

    rows: list[dict[str, Any]] = []
    # Filter out rows we'd skip downstream anyway (missing submitted_at) so
    # the sort doesn't crash on None vs tz-aware datetime mixing.
    eligible = [r for r in distributions if r.get("submitted_at") is not None]
    # Newest-first so when the cap fires we keep the most relevant rows.
    for fr in sorted(eligible, key=lambda r: r["submitted_at"], reverse=True):
        dist = fr.get("distribution")
        if not dist:
            continue
        bins = dist.get("bins") or []
        if len(bins) < 2:
            continue
        anchor_price = float(dist.get("anchor_price") or 0.0)
        if anchor_price <= 0:
            continue
        axis = dist.get("axis") or "return_pct"
        horizon = dist.get("horizon") or fr.get("horizon") or "1d"
        submitted_at = fr.get("submitted_at")
        if submitted_at is None:
            continue
        horizon_min = horizon_to_minutes(horizon)
        t_lo = submitted_at
        t_hi = submitted_at + _dt.timedelta(minutes=horizon_min)

        xs = [float(b["x"]) for b in bins]
        ps = [float(b["p"]) for b in bins]
        if axis == "log_return":
            # Linear small-x approx — same as the model_inputs validator path.
            xs_pct = [100.0 * x for x in xs]
        else:
            xs_pct = xs
        y_centers = [anchor_price * (1.0 + x / 100.0) for x in xs_pct]

        n = len(y_centers)
        for i in range(n):
            if i == 0:
                y_lo = y_clip_lo
            else:
                y_lo = (y_centers[i - 1] + y_centers[i]) / 2.0
            if i == n - 1:
                y_hi = y_clip_hi
            else:
                y_hi = (y_centers[i] + y_centers[i + 1]) / 2.0
            # Clip to chart range; skip bins entirely outside.
            y_lo_c = max(y_lo, y_clip_lo)
            y_hi_c = min(y_hi, y_clip_hi)
            if y_hi_c <= y_lo_c:
                continue
            alpha = max(_BAND_ALPHA_FLOOR, min(_BAND_ALPHA_CAP, ps[i]))
            rows.append({
                "t_lo": t_lo, "t_hi": t_hi,
                "y_lo": y_lo_c, "y_hi": y_hi_c,
                "p": ps[i],
                "alpha": alpha,
                "horizon": horizon,
                "submitted_at": submitted_at,
                "y_center": y_centers[i],
            })
            if len(rows) >= _BAND_ROW_CAP:
                return rows
    return rows


def _render_symbol_chart(symbol: str, bars: list[dict[str, Any]],
                         fills: list[dict[str, Any]],
                         distributions: list[dict[str, Any]] | None = None,
                         chart_key_suffix: str = "") -> None:
    """Per-symbol panel — Robinhood-style range picker (2W → 1Y), %return
    label, daily close line chart with green/red fill markers. Pan/zoom on
    the X axis only. `bars` is up to ~365 daily rows from local_bars_daily;
    the picker filters in-memory so no re-query on click.

    When `distributions` is passed, draws prediction-band rectangles behind
    the price line: one rectangle per (forecast × bin), color = single
    neutral, opacity proportional to bin probability `p`.

    `chart_key_suffix` ensures unique Streamlit keys when the same symbol is
    rendered across multiple per-agent panels under one ticker header."""
    window = _range_picker(key=f"sym_range_{symbol}{chart_key_suffix}", default="2W",
                           options=_SYMBOL_RANGE_OPTIONS)
    sel_bars = _filter_by_window(bars, window, time_key="bar_date")
    sel_fills = _filter_by_window(fills, window, time_key="filled_at")

    if len(sel_bars) < 2:
        st.info(f"Not enough daily bars in window — local_bars_daily has "
                f"{len(bars)} day(s) cached for {symbol}.")
        return

    _render_pct_label(sel_bars, time_key="bar_date",
                      value_key="close", count_suffix="days")

    import pandas as pd
    bar_df = pd.DataFrame([{
        "bar_date": b["bar_date"],
        "close": float(b["close"]),
    } for b in sel_bars])
    try:
        import altair as alt
        line = alt.Chart(bar_df).mark_line().encode(
            x=alt.X("bar_date:T", title=None),
            y=alt.Y("close:Q", title=None,
                    scale=alt.Scale(zero=False)),
            tooltip=["bar_date:T", "close:Q"],
        ).properties(height=180)

        # Click selection bound to fill markers only — clicking the price
        # line does nothing. Captured below via on_select="rerun".
        fill_select = alt.selection_point(
            fields=["filled_at"], on="click", nearest=True, empty=False,
            name="fill_select",
        )

        # Prediction-band layer (Phase 3). Drawn FIRST so the price line +
        # triangles paint on top. Y-clip derived from the visible bars to keep
        # outer (±∞) bins from blowing the chart's y-scale.
        band_layers: list = []
        if distributions:
            closes = [float(b["close"]) for b in sel_bars]
            y_clip_lo = min(closes) * 0.95
            y_clip_hi = max(closes) * 1.05
            # Restrict to distributions whose submitted_at lies in the window
            # — they're rendered with a t_hi extending past the window which is
            # fine for visibility, but we don't want ancient distributions
            # cluttering a recent zoom.
            window_lo_dt = bar_df["bar_date"].min()
            in_window = [d for d in distributions
                         if d.get("submitted_at") is not None
                         and d.get("submitted_at") >= window_lo_dt]
            band_rows = _distribution_band_rows(in_window, y_clip_lo, y_clip_hi)
            if band_rows:
                band_df = pd.DataFrame(band_rows)
                band_layers.append(alt.Chart(band_df).mark_rect().encode(
                    x="t_lo:T", x2="t_hi:T",
                    y="y_lo:Q", y2="y_hi:Q",
                    opacity=alt.Opacity(
                        "alpha:Q",
                        scale=alt.Scale(domain=[_BAND_ALPHA_FLOOR, _BAND_ALPHA_CAP],
                                        range=[_BAND_ALPHA_FLOOR, _BAND_ALPHA_CAP]),
                        legend=None,
                    ),
                    color=alt.value("#4c78a8"),
                    tooltip=[
                        alt.Tooltip("submitted_at:T", title="submitted"),
                        alt.Tooltip("horizon:N", title="horizon"),
                        alt.Tooltip("y_lo:Q", title="y_lo", format=".2f"),
                        alt.Tooltip("y_hi:Q", title="y_hi", format=".2f"),
                        alt.Tooltip("p:Q", title="p", format=".3f"),
                    ],
                ))
                if len(band_rows) >= _BAND_ROW_CAP:
                    st.caption(
                        f"⚠ band cap hit: showing {_BAND_ROW_CAP} most-recent "
                        f"rectangles; older forecasts elided"
                    )

        layers = band_layers + [line]
        if sel_fills:
            # IBKR's `fills.action` is "BOT" / "SLD"; tolerate "BUY" / "SELL"
            # from any future synthetic source so this is robust either way.
            _BUY = {"BOT", "BUY"}
            _SELL = {"SLD", "SELL"}
            fill_df = pd.DataFrame([{
                "filled_at": f["filled_at"],
                "fill_price": float(f["fill_price"]),
                "action": (f["action"] or "").upper(),
                "quantity": float(f["quantity"]),
                "notional": abs(float(f["quantity"]) * float(f["fill_price"])),
                "notional_label": _fmt_money_compact(
                    abs(float(f["quantity"]) * float(f["fill_price"]))),
            } for f in sel_fills])
            buys = fill_df[fill_df["action"].isin(_BUY)]
            sells = fill_df[fill_df["action"].isin(_SELL)]
            if not buys.empty:
                layers.append(alt.Chart(buys).mark_point(
                    shape="triangle-up", size=160, color="#2ca02c",
                    filled=True,
                ).encode(
                    x="filled_at:T", y="fill_price:Q",
                    tooltip=["filled_at:T", "fill_price:Q",
                             "quantity:Q", "notional:Q"],
                ).add_params(fill_select))
                # Buy notional sits ABOVE the up-triangle.
                layers.append(alt.Chart(buys).mark_text(
                    align="center", baseline="bottom", dy=-10,
                    fontSize=11, fontWeight="bold", color="#2ca02c",
                ).encode(
                    x="filled_at:T", y="fill_price:Q",
                    text="notional_label:N",
                ))
            if not sells.empty:
                layers.append(alt.Chart(sells).mark_point(
                    shape="triangle-down", size=160, color="#d62728",
                    filled=True,
                ).encode(
                    x="filled_at:T", y="fill_price:Q",
                    tooltip=["filled_at:T", "fill_price:Q",
                             "quantity:Q", "notional:Q"],
                ).add_params(fill_select))
                # Sell notional sits BELOW the down-triangle.
                layers.append(alt.Chart(sells).mark_text(
                    align="center", baseline="top", dy=10,
                    fontSize=11, fontWeight="bold", color="#d62728",
                ).encode(
                    x="filled_at:T", y="fill_price:Q",
                    text="notional_label:N",
                ))
        composed = alt.layer(*layers).add_params(_interactive_x())

        result = st.altair_chart(
            composed, use_container_width=True,
            key=f"fill_chart_{symbol}{chart_key_suffix}", on_select="rerun",
        )

        # Click capture → session_state. Streamlit returns a VegaLiteState
        # whose `.selection` is a dict keyed by selection name. We only
        # care about `fill_select`; pop the latest clicked point's
        # `filled_at` and stash (symbol, iso) so the panel re-renders.
        if result is not None and getattr(result, "selection", None):
            pts = (result.selection.get("fill_select") or [])
            if pts:
                clicked_iso = pts[0].get("filled_at")
                if clicked_iso:
                    # Vega-Lite serializes the time field as ms-since-epoch
                    # if not stringified; coerce to ISO for the SQL query.
                    if isinstance(clicked_iso, (int, float)):
                        from datetime import datetime, timezone
                        clicked_iso = datetime.fromtimestamp(
                            float(clicked_iso) / 1000, tz=timezone.utc,
                        ).isoformat()
                    new_sel = (symbol, str(clicked_iso))
                    if st.session_state.get("live_trace_selected_fill") != new_sel:
                        st.session_state["live_trace_selected_fill"] = new_sel
                        st.rerun()
    except ImportError:
        st.line_chart(bar_df.set_index("bar_date")["close"], height=180)
        if sel_fills:
            st.caption(f"{len(sel_fills)} fills in window (chart markers require altair)")


# ── Fill context side panel ──────────────────────────────────────────────────

def _render_fill_context_panel(selected: tuple[str, str]) -> None:
    """Right-side panel content. `selected = (symbol, filled_at_iso)` stored
    by `_render_symbol_chart` whenever a fill triangle is clicked."""
    symbol, filled_at = selected
    ctx = _cached_fill_context(symbol, filled_at)
    if not ctx:
        st.warning(f"No fill found at {filled_at} for {symbol}.")
        return
    if ctx.get("error"):
        st.error(f"Lookup failed: {ctx['error']}")
        return

    fill = ctx["fill"]
    action = (fill.get("action") or "").upper()
    arrow = "🟢" if action in {"BOT", "BUY"} else "🔴"
    notional = abs(float(fill["quantity"]) * float(fill["fill_price"]))
    st.markdown(
        f"#### {arrow} {action} {float(fill['quantity']):.0f} "
        f"{fill['symbol']} @ ${float(fill['fill_price']):,.2f}"
    )
    st.caption(
        f"{_fmt_time(fill['filled_at'])}  ·  notional ${notional:,.0f}  ·  "
        f"order #{fill.get('order_id') or '—'}"
    )

    decision = ctx.get("decision")
    if decision:
        st.caption(
            f"Allocator decision #{decision['id']}  ·  "
            f"NAV ${float(decision['nav_at_decision'] or 0):,.0f}  ·  "
            f"{decision.get('notes') or ''}"
        )

    contributors = ctx.get("contributors") or []
    attribution_source = ctx.get("attribution_source") or "ledger"

    if not contributors:
        st.info(
            "**Orphan fill** — no contributing sector agent. Likely a "
            "manual trade, kill-switch close, or pre-system position. "
            "Mike's allocator placed the order without per-agent attribution. "
            "No nearby allocation_decision found within ±90s either."
        )
        return

    if attribution_source == "inferred_by_time":
        st.warning(
            "**Inferred attribution** — no `agent_ledger` rows back this fill; "
            "contributors below were reconstructed from the nearest "
            "allocation_decision (±90s window) and represent the convictions "
            "that LED to the allocator placing the order, not actual lent qty."
        )

    if len(contributors) == 1:
        _render_one_contributor(contributors[0])
    else:
        tabs = st.tabs([
            _contributor_tab_label(c) for c in contributors
        ])
        for t, c in zip(tabs, contributors):
            with t:
                _render_one_contributor(c)


def _contributor_tab_label(c: dict[str, Any]) -> str:
    """Tab label per contributor. For ledger rows, show the lent qty; for
    inferred rows, show the conviction weight that drove the decision."""
    agent = c.get("agent_name", "?")
    qty = c.get("ledger_qty") or 0.0
    if qty:
        return f"{agent}  ({qty:.0f})"
    w = c.get("inferred_weight") or 0.0
    return f"{agent}  (w={w:+.2f})"


def _render_one_contributor(c: dict[str, Any]) -> None:
    """Render one contributing agent's full context inside the panel."""
    # 1. Ledger row — small caption-row at the top, no expander.
    # Inferred contributors (no ledger row) have ledger_event=None — render
    # the inferred conviction weight instead so the contributor strip stays
    # informative regardless of attribution_source.
    if c.get("ledger_event"):
        pnl = c.get("ledger_pnl")
        pnl_str = (f"realized {pnl:+,.2f}" if pnl is not None else "no realized P&L")
        st.markdown(
            f"**{c['ledger_event']}**  ·  {c['ledger_qty']:.2f} shares @ "
            f"${c['ledger_price']:.2f}  ·  {pnl_str}"
        )
    else:
        weight = c.get("inferred_weight") or 0.0
        st.markdown(
            f"**INFERRED**  ·  weight {weight:+.3f} in the originating "
            f"allocation_decision  ·  no lent qty (no ledger row)"
        )

    # 2. Conviction at fill time
    conv = c.get("conviction")
    with st.expander("Conviction", expanded=True):
        if not conv:
            st.caption(
                "No conviction row alive at fill time. "
                "Convictions are upsert-replaced — historical reasoning may "
                "have been overwritten."
            )
        else:
            cols = st.columns(3)
            cols[0].metric("Direction", conv["direction"])
            cols[1].metric("Conviction", f"{float(conv['conviction']):.2f}")
            cols[2].metric(
                "E[return] %",
                f"{float(conv['expected_return_pct']):+.2f}"
                if conv.get("expected_return_pct") is not None else "—",
            )
            if conv.get("time_to_target_days") is not None:
                st.caption(f"horizon: {conv['time_to_target_days']} days  ·  "
                           f"submitted {_fmt_time(conv['submitted_at'])}")
            if conv.get("rationale"):
                st.markdown("**Rationale**")
                st.markdown(conv["rationale"])
            mi = conv.get("model_inputs")
            if mi:
                st.markdown("**Model inputs**")
                if isinstance(mi, str):
                    try:
                        mi = json.loads(mi)
                    except json.JSONDecodeError:
                        pass
                st.json(mi)

    # 3. Open theses on this symbol
    theses = c.get("theses") or []
    if theses:
        with st.expander(f"Active theses ({len(theses)})", expanded=False):
            for t in theses:
                st.markdown(f"**{t['title']}**  ·  _{t['kind']}_  ·  "
                            f"verify by {t.get('verify_by') or '—'}")
                if t.get("body"):
                    st.markdown(t["body"])
                st.divider()

    # 4. Originating session
    session = c.get("session")
    with st.expander("Originating session", expanded=True):
        if not session:
            st.caption("No audit_log row found within the lookup window.")
        else:
            st.caption(
                f"session {session['session_id'][:8]}  ·  "
                f"skill {session.get('skill_name') or session.get('routine')}  ·  "
                f"{session.get('tool_rounds') or 0} tool rounds  ·  "
                f"{_fmt_time(session['created_at'])}"
            )
            if session.get("thinking_block"):
                st.markdown("**Thinking**")
                st.code(session["thinking_block"][:8000],
                        language="markdown")
            if session.get("final_response"):
                st.markdown("**Final response**")
                st.markdown(session["final_response"][:8000])
            if session.get("system_prompt"):
                with st.expander("System prompt", expanded=False):
                    st.code(session["system_prompt"][:8000],
                            language="markdown")

    # 5. Tool calls — each in its own nested expander.
    tool_calls = c.get("tool_calls") or []
    with st.expander(f"Tool calls ({len(tool_calls)})", expanded=False):
        if not tool_calls:
            st.caption("No tool calls recorded for the session.")
        for t in tool_calls:
            err = f"  ·  ⚠ {t['error']}" if t.get("error") else ""
            label = (f"[{t.get('tool_round')}]  {t['tool_name']}  ·  "
                     f"{t.get('duration_ms') or 0}ms{err}")
            with st.expander(label, expanded=False):
                if t.get("tool_input"):
                    st.markdown("_input_")
                    st.code(str(t["tool_input"])[:8000], language="json")
                if t.get("tool_output"):
                    st.markdown("_output_")
                    out = str(t["tool_output"])
                    suffix = " …(truncated at 8 kB)" if len(out) > 8000 else ""
                    st.code(out[:8000] + suffix, language="json")


@st.cache_data(ttl=60)
def _live_agent_skill(window_days: int) -> list[dict[str, Any]]:
    try:
        from obs.queries import agent_skill_by_horizon
        return agent_skill_by_horizon(window_days)
    except Exception as e:
        log.warning("agent_skill query failed: %s", e)
        return []


@st.cache_data(ttl=60)
def _live_model_skill(window_days: int) -> list[dict[str, Any]]:
    try:
        from obs.queries import model_skill_by_horizon
        return model_skill_by_horizon(window_days)
    except Exception as e:
        log.warning("model_skill query failed: %s", e)
        return []


@st.cache_data(ttl=60)
def _live_calibration_curve(agent: str | None, model: str | None,
                            horizon: str | None, window_days: int) -> list[dict[str, Any]]:
    try:
        from obs.queries import calibration_curve
        return calibration_curve(agent=agent, model=model,
                                 horizon=horizon, window_days=window_days)
    except Exception as e:
        log.warning("calibration_curve query failed: %s", e)
        return []


def render_model_skill() -> None:
    """Model Skill tab — Brier / log-loss / CRPS / pinball / Sharpe-of-skill
    per (agent, horizon) and (agent, model, horizon), plus a reliability
    curve per (model, horizon). Reads agent_forecast rows whose distribution
    has been resolved + scored by scripts/run_forecast_resolver.py.

    Empty for the first horizon-length × ~3 trading days after distribution
    submission begins — there's no calibration without resolved outcomes.
    """
    st.subheader("Model skill — calibration over recent forecasts")

    window_days = st.slider("Window (days)", min_value=7, max_value=90,
                             value=30, step=1)

    agent_rows = _live_agent_skill(window_days)
    if not agent_rows:
        st.info(
            "No scored distributions yet in the window. "
            "Run `python scripts/run_forecast_resolver.py` after at least one "
            "distribution's horizon has elapsed (5m for the OU model)."
        )
        return

    # ─ Per-agent table ─────────────────────────────────────────────────────
    st.markdown("**Per agent × horizon** "
                "(lower log-loss/Brier/CRPS = better; higher Sharpe-of-skill = better)")
    formatted = [
        {
            "agent": r["agent_name"],
            "horizon": r["horizon"],
            "n": int(r["n"]),
            "logloss":  f"{float(r['mean_logloss']):.3f}",
            "brier":    f"{float(r['mean_brier']):.3f}",
            "crps":     f"{float(r['mean_crps']):.3f}",
            "pin05":    f"{float(r['mean_pinball05']):.4f}",
            "pin95":    f"{float(r['mean_pinball95']):.4f}",
            "mean_edge": f"{float(r['mean_signed_edge']):+.3f}",
            "sharpe":   f"{float(r['sharpe_of_skill']):+.2f}",
        }
        for r in agent_rows
    ]
    st.dataframe(formatted, use_container_width=True, hide_index=True)
    st.divider()

    # ─ Per-model table ────────────────────────────────────────────────────
    model_rows = _live_model_skill(window_days)
    if model_rows:
        st.markdown("**Per agent × model × horizon**")
        m_fmt = [
            {
                "agent": r["agent_name"],
                "model": f"{r['model']} v{r['model_version']}",
                "horizon": r["horizon"],
                "n": int(r["n"]),
                "logloss":  f"{float(r['mean_logloss']):.3f}",
                "brier":    f"{float(r['mean_brier']):.3f}",
                "crps":     f"{float(r['mean_crps']):.3f}",
                "sharpe":   f"{float(r['sharpe_of_skill']):+.2f}",
            }
            for r in model_rows
        ]
        st.dataframe(m_fmt, use_container_width=True, hide_index=True)
        st.divider()

    # ─ Calibration curve ──────────────────────────────────────────────────
    st.markdown("**Reliability curve** (predicted bin probability vs realized hit rate)")
    agents_avail = sorted({r["agent_name"] for r in agent_rows})
    models_avail = sorted({r["model"] for r in model_rows}) if model_rows else []
    horizons_avail = sorted({r["horizon"] for r in agent_rows})

    sel_cols = st.columns(3)
    with sel_cols[0]:
        sel_agent = st.selectbox("Agent", ["(any)"] + agents_avail, index=0)
    with sel_cols[1]:
        sel_model = st.selectbox("Model", ["(any)"] + models_avail, index=0)
    with sel_cols[2]:
        sel_horizon = st.selectbox("Horizon", ["(any)"] + horizons_avail, index=0)

    curve = _live_calibration_curve(
        agent=None if sel_agent == "(any)" else sel_agent,
        model=None if sel_model == "(any)" else sel_model,
        horizon=None if sel_horizon == "(any)" else sel_horizon,
        window_days=window_days,
    )
    if not curve:
        st.caption("Not enough bin-level data for the chosen filters (need ≥5 per bucket).")
        return

    # Render as Altair scatter on a diagonal (perfect calibration line).
    import altair as alt
    import pandas as pd
    df = pd.DataFrame(curve)
    chart = (
        alt.Chart(df)
        .mark_circle(size=120, opacity=0.75)
        .encode(
            x=alt.X("predicted_mid:Q", title="Predicted probability",
                    scale=alt.Scale(domain=[0, 1])),
            y=alt.Y("realized_rate:Q", title="Realized hit rate",
                    scale=alt.Scale(domain=[0, 1])),
            size=alt.Size("n_predictions:Q", title="n bins"),
            tooltip=["predicted_low", "predicted_high", "realized_rate", "n_predictions"],
        )
    )
    diag = (
        alt.Chart(pd.DataFrame({"x": [0, 1], "y": [0, 1]}))
        .mark_line(strokeDash=[4, 4], color="#888")
        .encode(x="x:Q", y="y:Q")
    )
    st.altair_chart(diag + chart, use_container_width=True)
    st.caption(
        "Points above the diagonal = under-confident bins; below = over-confident. "
        "Bubble size = number of bins contributing to that bucket."
    )


def main() -> None:
    # URL-driven routing — Streamlit's st.tabs() doesn't sync with query_params,
    # so we use ?view=grid|detail|live_trace|skill and render exactly one
    # view per page. Stale ?view=diff bookmarks fall through to grid.
    view = st.query_params.get("view", "grid")
    if "session" in st.query_params and view == "grid":
        view = "detail"  # auto-jump when a session is selected from a card click

    st.title("📡 Trading desk · live agents")

    nav_cols = st.columns([1, 1, 1, 1, 3])
    with nav_cols[0]:
        if st.button("🔴 Live grid", use_container_width=True,
                     type="primary" if view == "grid" else "secondary"):
            st.query_params.clear()
            st.rerun()
    with nav_cols[1]:
        if st.button("🔍 Skill detail", use_container_width=True,
                     type="primary" if view == "detail" else "secondary"):
            st.query_params.clear()
            st.query_params["view"] = "detail"
            st.rerun()
    with nav_cols[2]:
        if st.button("📈 Live trace", use_container_width=True,
                     type="primary" if view == "live_trace" else "secondary"):
            st.query_params.clear()
            st.query_params["view"] = "live_trace"
            st.rerun()
    with nav_cols[3]:
        if st.button("🎯 Model skill", use_container_width=True,
                     type="primary" if view == "skill" else "secondary"):
            st.query_params.clear()
            st.query_params["view"] = "skill"
            st.rerun()
    st.divider()

    if view == "grid":
        render_live_grid()
    elif view == "detail":
        render_skill_detail()
    elif view == "live_trace":
        render_live_trace()
    elif view == "skill":
        render_model_skill()
    else:
        render_live_grid()


if __name__ == "__main__":
    main()
else:
    # Streamlit imports the module directly
    main()
