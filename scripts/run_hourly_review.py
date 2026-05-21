#!/usr/bin/env python3
"""Programmatic hourly desk heartbeat. Replaces the `/hourly-review` LLM skill.

Triggered by the orchestrator's phase 3 every hour. AZ quiet window
(22:00-05:00 MST) → silent exit (no Telegram, no spam). Otherwise: read
desk state, compose a deterministic heartbeat, ask the local vLLM for the
one-sentence "Watch:" line, send one Telegram.

Same template as the LLM skill (NAV/cash/positions footer, fills/orders/risk
header, single Watch line). The LLM never decides whether to trade, when to
fire, or what data to read — only the closing narrative sentence.

Exit codes:
    0  ran cleanly (sent Telegram, or quiet-window silent exit)
    1  unexpected error
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import find_dotenv, load_dotenv
    found = find_dotenv(usecwd=True)
    if found:
        load_dotenv(found)
except Exception:
    pass


def _setup_logging() -> logging.Logger:
    # Honor LOG_DIR for test isolation (conftest redirects to tmp).
    log_dir = Path(os.environ.get("LOG_DIR") or (_REPO_ROOT / "logs"))
    log_path = log_dir / "hourly-review.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("hourly_review")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(log_path)
        fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(fh)
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(sh)
    return logger


log = _setup_logging()

AZ = ZoneInfo("America/Phoenix")
ET = ZoneInfo("America/New_York")


def _now_et_str() -> str:
    return datetime.now(ET).strftime("%H:%M ET")


def _quiet_window_active() -> bool:
    h = datetime.now(AZ).hour
    return h >= 22 or h < 5


def _market_mode_emoji(market: dict) -> str:
    if not market.get("is_open"):
        return "⚫"
    return "🟡" if market.get("is_half_day") else "🟢"


def _summarize_fills(fills: list[dict]) -> tuple[int, str]:
    """Return (count, short summary like 'added LMT/LRCX, trimmed VOO').

    Buckets fills by direction:
      - BUY/BOT → 'added <symbol>'
      - SELL/SLD that closes out → 'closed <symbol>'
      - SELL/SLD that doesn't close → 'trimmed <symbol>'

    We don't have position state to verify close-vs-trim here, so anything
    SELL is grouped as 'trimmed' and the summary stays short. Callers needing
    precise close detection should diff against positions_anchor.
    """
    if not fills:
        return 0, "no new positions"
    added: set[str] = set()
    trimmed: set[str] = set()
    for f in fills:
        action = (f.get("action") or "").upper()
        sym = (f.get("symbol") or "").upper()
        if not sym:
            continue
        if action in ("BUY", "BOT"):
            added.add(sym)
        elif action in ("SELL", "SLD"):
            trimmed.add(sym)
    bits = []
    if added:
        bits.append("added " + "/".join(sorted(added)[:5]))
    if trimmed:
        bits.append("trimmed " + "/".join(sorted(trimmed)[:5]))
    summary = "; ".join(bits) or "no new positions"
    return len(fills), summary


def _short_heartbeat_if_quiet(
    market: dict, balances: dict, positions: list[dict],
) -> str:
    """The 2-line 'quiet hour, no changes' form from the skill spec."""
    nav = float(balances.get("nav") or 0.0)
    cash = float(balances.get("cash") or 0.0)
    cash_pct = (cash / nav * 100) if nav > 0 else 0
    n_positions = sum(1 for p in positions if float(p.get("quantity") or 0) != 0)
    return (
        f"{_market_mode_emoji(market)} *Heartbeat — {_now_et_str()}* — quiet hour, no changes.\n"
        f"NAV ${nav:,.0f} · cash {cash_pct:.0f}% · {n_positions} positions."
    )


def _compose_heartbeat(
    market: dict, ks: dict, balances: dict, positions: list[dict],
    open_orders: list[dict], pnl_windows: dict, proposals: list[dict],
    fills_last_hour: list[dict], watch: str,
) -> str:
    """Full heartbeat body, ≤700 chars, Markdown per the skill spec.

    `pnl_windows` is the get_agent_pnl_windows payload — day P&L is the
    `desk.today.pnl_usd` delta (since today's 09:30 ET open).
    """
    n_fills, fill_summary = _summarize_fills(fills_last_hour)
    n_orders_placed_this_hour = len({f.get("order_id") for f in fills_last_hour if f.get("order_id")})
    n_open_orders = len(open_orders or [])
    n_pending = len(proposals or [])
    n_positions = sum(1 for p in positions if float(p.get("quantity") or 0) != 0)

    nav = float(balances.get("nav") or 0.0)
    cash = float(balances.get("cash") or 0.0)
    cash_pct = (cash / nav * 100) if nav > 0 else 0

    desk_w = (pnl_windows.get("desk") or {})
    today_d = (desk_w.get("today") or {}).get("pnl_usd")
    today_str = f"${today_d:,.0f}" if today_d is not None else "n/a"
    kill_str = "active" if (ks.get("global_kill") or ks.get("per_agent", {}).get("mike")) else "ok"

    body = (
        f"{_market_mode_emoji(market)} *Heartbeat — {_now_et_str()}*\n"
        f"*New this hour:* {n_fills} fills · {fill_summary}\n"
        f"*Pending:* {n_open_orders} working orders · {n_pending} approval-gated\n"
        f"*Risk:* kill={kill_str} · day P&L={today_str}\n"
        f"*Watch:* {watch}\n"
        f"NAV ${nav:,.0f} · cash {cash_pct:.0f}% · {n_positions} positions"
    )
    return body[:700]


def _template_watch(fills: list[dict], proposals: list[dict], ks: dict) -> str:
    """Deterministic fallback if vLLM isn't reachable."""
    if ks.get("global_kill") or ks.get("per_agent", {}).get("mike"):
        return "kill switch is active — manual intervention required"
    if not fills and not proposals:
        return "nothing concerning"
    if proposals:
        return f"{len(proposals)} approval-gated proposal(s) awaiting your reply"
    return f"{len(fills)} fill(s) this hour"


async def _llm_watch_sentence(
    market: dict, fills: list[dict], pnl_windows: dict, proposals: list[dict], ks: dict,
) -> str:
    """Ask the local vLLM for the 'Watch:' sentence. Hard timeout 25s.

    `pnl_windows` is the get_agent_pnl_windows payload — day P&L is the
    `desk.today.pnl_usd` delta (since today's 09:30 ET open).
    """
    from pipelines.llm_client import make_client

    n_fills, fill_summary = _summarize_fills(fills)
    desk_w = (pnl_windows.get("desk") or {})
    today_d = (desk_w.get("today") or {}).get("pnl_usd")
    today_str = f"${today_d:,.0f}" if today_d is not None else "n/a"
    kill_active = ks.get("global_kill") or ks.get("per_agent", {}).get("mike")
    pending_count = len(proposals or [])

    prompt = (
        "/no_think\n"
        "Write ONE sentence (<=120 chars, no preamble, no quotes, no markdown) describing "
        "the most notable thing this hour for the desk. Single most important watch item — "
        "earnings, risk flag, regime shift, or 'nothing concerning' if quiet.\n\n"
        f"Market: open={market.get('is_open')} mode={market.get('mode')}\n"
        f"Fills this hour: {n_fills} ({fill_summary})\n"
        f"Day P&L: {today_str}\n"
        f"Kill switch active: {kill_active}\n"
        f"Pending approvals: {pending_count}\n"
    )

    client = make_client(skill_name="hourly-review-watch")
    resp = await asyncio.wait_for(
        client.client.chat.completions.create(
            model=client.model,
            max_tokens=150,
            messages=[
                {"role": "system", "content": "You write one-sentence heartbeat watches for a trading desk. Output the sentence only."},
                {"role": "user", "content": prompt},
            ],
        ),
        timeout=25.0,
    )
    text = (resp.choices[0].message.content or "").strip()
    if "<think>" in text and "</think>" in text:
        text = text.split("</think>", 1)[1].strip()
    text = text.strip('"\' ').replace("\n", " ")
    return text[:200] or "nothing concerning"


async def _load_state() -> dict:
    """Read every data source the heartbeat needs. Errors here are non-fatal —
    a partial heartbeat is better than no heartbeat."""
    import mcp_server
    from db import store

    state: dict = {}

    async def _safe(name: str, fn):
        try:
            state[name] = json.loads(await fn())
        except Exception as exc:
            log.warning("load_state: %s failed: %s", name, exc)
            state[name] = {}

    await _safe("market", mcp_server.get_market_status)
    await _safe("kill_switch", mcp_server.get_kill_switch_status)
    await _safe("balances", mcp_server.get_balances)
    await _safe("positions_raw", mcp_server.get_positions)
    await _safe("open_orders_raw", mcp_server.get_open_orders)
    await _safe("pnl_windows", mcp_server.get_agent_pnl_windows)
    await _safe("proposals_raw", mcp_server.list_pending_proposals)

    # Normalise list-shapes — MCP tools return JSON objects, not bare arrays.
    state["positions"] = (state.get("positions_raw") or {}).get("positions") or []
    state["open_orders"] = (state.get("open_orders_raw") or {}).get("orders") or []
    state["proposals"] = (state.get("proposals_raw") or {}).get("pending") or []

    # Recent fills via db.store
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    try:
        state["fills_last_hour"] = await store.get_fills_window(since=since)
    except Exception as exc:
        log.warning("load_state: fills_last_hour failed: %s", exc)
        state["fills_last_hour"] = []

    return state


async def main() -> int:
    start = time.time()
    log.info("hourly-review runner starting")

    if _quiet_window_active():
        h = datetime.now(AZ).hour
        log.info("quiet window (AZ hour=%d) — silent exit (no Telegram)", h)
        return 0

    state = await _load_state()
    market = state.get("market") or {}
    ks = state.get("kill_switch") or {}
    balances = state.get("balances") or {}
    positions = state.get("positions") or []
    open_orders = state.get("open_orders") or []
    pnl_windows = state.get("pnl_windows") or {}
    proposals = state.get("proposals") or []
    fills = state.get("fills_last_hour") or []

    # Short form: no fills, no pending, kill ok → 2-line heartbeat
    kill_active = ks.get("global_kill") or ks.get("per_agent", {}).get("mike")
    has_activity = bool(fills) or bool(open_orders) or bool(proposals) or kill_active
    if not has_activity:
        body = _short_heartbeat_if_quiet(market, balances, positions)
        log.info("no activity — sending short heartbeat")
    else:
        try:
            watch = await _llm_watch_sentence(market, fills, pnl_windows, proposals, ks)
            log.info("watch (LLM): %s", watch)
        except Exception as exc:
            log.warning("LLM watch-sentence failed: %s — using template", exc)
            watch = _template_watch(fills, proposals, ks)
        body = _compose_heartbeat(market, ks, balances, positions, open_orders,
                                  pnl_windows, proposals, fills, watch)
        log.info("composed heartbeat (%d chars, %d fills, %d open orders)",
                 len(body), len(fills), len(open_orders))

    from approval.telegram import send_message
    try:
        await send_message(
            body, kind="push",
            meta={"author_agent": "hourly-review"},
            source_ref={"kind": "agent_push", "author_agent": "hourly-review",
                        "subkind": "heartbeat"},
        )
    except Exception as exc:
        log.error("telegram send failed: %s", exc)
        return 1

    log.info("hourly-review done in %.1fs", time.time() - start)
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    except Exception:
        log.exception("runner crashed")
        rc = 1
    sys.exit(rc)
