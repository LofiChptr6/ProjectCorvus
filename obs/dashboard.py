"""Streamlit dashboard for the local-LLM agent fleet.

Three tabs:
  1. Live grid       — agent cards with status + recent runs
  2. Skill detail    — full conversation viewer for one session, with live tile
                       embedded if the session is still running
  3. Diff (A vs B)   — side-by-side comparison of two runs of the same skill

Reads from Postgres (audit_log + tool_calls) populated by obs/proxy.py.

Run:
    .venv/bin/streamlit run obs/dashboard.py --server.address 127.0.0.1 --server.port 8501
"""

from __future__ import annotations

import datetime as dt
import difflib
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


# ── Tab 1: Live grid ──────────────────────────────────────────────────────────


def render_live_grid() -> None:
    st_autorefresh(interval=2000, key="grid_autorefresh")
    live = {s["agent"]: s for s in _live_snapshot()}

    st.caption(
        f"Last refresh: {dt.datetime.now().strftime('%H:%M:%S')}  ·  "
        f"Live sessions: {len(live)}  ·  Proxy: {PROXY_URL}"
    )

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


# ── Tab 3: Diff ───────────────────────────────────────────────────────────────


def render_diff() -> None:
    agents = queries.list_known_agents()
    if not agents:
        st.info("No data yet. Run at least two skill invocations to compare.")
        return

    agent = st.selectbox("Agent", agents, key="diff_agent")
    skills = _skills_for_agent(agent)
    if not skills:
        st.info(f"No skills logged for {agent} yet.")
        return
    skill = st.selectbox("Skill", skills, key="diff_skill")
    sessions = _sessions_for_skill(agent, skill, limit=20)
    if len(sessions) < 2:
        st.info("Need ≥ 2 runs of this skill to diff. Run it again.")
        return

    labels = [
        f"{s['session_id'][:8]} · {_fmt_time(s.get('started_at'))} · "
        f"{_fmt_ms(s.get('duration_ms'))} · "
        f"{_fmt_tokens((s.get('prompt_tokens') or 0) + (s.get('completion_tokens') or 0))} tok"
        + ("  ⚠" if s.get("had_error") else "")
        for s in sessions
    ]
    sid_by_label = dict(zip(labels, [s["session_id"] for s in sessions]))

    c1, c2 = st.columns(2)
    with c1:
        a_label = st.selectbox("A (older)", labels, index=min(1, len(labels) - 1), key="diff_a")
    with c2:
        b_label = st.selectbox("B (newer)", labels, index=0, key="diff_b")

    if not a_label or not b_label or a_label == b_label:
        st.warning("Pick two different sessions.")
        return

    sid_a = sid_by_label[a_label]
    sid_b = sid_by_label[b_label]
    ex_a, tc_a = _session_detail(sid_a)
    ex_b, tc_b = _session_detail(sid_b)

    st.markdown("#### Tool-call set diff")
    tools_a = {(tc["tool_name"], json.dumps(tc.get("tool_input_parsed", {}), sort_keys=True))
               for tc in tc_a}
    tools_b = {(tc["tool_name"], json.dumps(tc.get("tool_input_parsed", {}), sort_keys=True))
               for tc in tc_b}
    only_a = sorted(tools_a - tools_b)
    only_b = sorted(tools_b - tools_a)
    common = sorted(tools_a & tools_b)
    cc1, cc2, cc3 = st.columns(3)
    with cc1:
        st.markdown(f"**A only** ({len(only_a)})")
        for n, inp in only_a[:50]:
            st.code(f"{n}\n{inp[:300]}", language="json")
    with cc2:
        st.markdown(f"**Common** ({len(common)})")
        for n, _ in common[:50]:
            st.text(n)
    with cc3:
        st.markdown(f"**B only** ({len(only_b)})")
        for n, inp in only_b[:50]:
            st.code(f"{n}\n{inp[:300]}", language="json")

    st.markdown("#### Final response diff")
    final_a = (ex_a[-1].get("final_response") if ex_a else "") or ""
    final_b = (ex_b[-1].get("final_response") if ex_b else "") or ""
    diff = "\n".join(difflib.unified_diff(
        final_a.splitlines(), final_b.splitlines(),
        fromfile=a_label, tofile=b_label, lineterm="",
    ))
    if diff:
        st.code(diff, language="diff")
    else:
        st.success("Final responses are identical.")

    with st.expander(f"Full message JSON · A ({sid_a[:8]})", expanded=False):
        st.json([e.get("messages_parsed", []) for e in ex_a])
    with st.expander(f"Full message JSON · B ({sid_b[:8]})", expanded=False):
        st.json([e.get("messages_parsed", []) for e in ex_b])


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    # URL-driven routing — Streamlit's st.tabs() doesn't sync with query_params,
    # so we use ?view=grid|detail|diff and render exactly one view per page.
    view = st.query_params.get("view", "grid")
    if "session" in st.query_params and view == "grid":
        view = "detail"  # auto-jump when a session is selected from a card click

    st.title("📡 Trading desk · live agents")

    nav_cols = st.columns([1, 1, 1, 4])
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
        if st.button("↔ Diff", use_container_width=True,
                     type="primary" if view == "diff" else "secondary"):
            st.query_params.clear()
            st.query_params["view"] = "diff"
            st.rerun()
    st.divider()

    if view == "grid":
        render_live_grid()
    elif view == "detail":
        render_skill_detail()
    elif view == "diff":
        render_diff()
    else:
        render_live_grid()


if __name__ == "__main__":
    main()
else:
    # Streamlit imports the module directly
    main()
