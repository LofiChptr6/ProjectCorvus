#!/usr/bin/env python3
"""Programmatic mike-allocator. Replaces the LLM-driven `/mike-allocator` skill.

Triggered hourly by scripts/run_hourly_orchestrator.sh (phase 2). All trading
decisions (sizing, risk-checks, sub-10-share gate, inverse-ETF netting,
approval gating) are already inside `rebalance_desk` in mcp_server.py — this
script just wires the guards, calls it, and posts the Telegram summary.

The LLM is invoked exactly once, AFTER orders are placed, to write the
one-sentence "why" line for the Telegram. A model bug there cannot affect
trading; on LLM failure we fall back to a deterministic template.

Exit codes:
    0  ran successfully (placed orders, sent telegram, or politely no-action)
    1  unexpected error during run
    2  skipped by guard (market closed / kill switch / quiet window)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
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
    log_path = _REPO_ROOT / "logs" / "mike-allocator.log"
    log_path.parent.mkdir(exist_ok=True)
    logger = logging.getLogger("mike_allocator")
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

ET = ZoneInfo("America/New_York")
AZ = ZoneInfo("America/Phoenix")


def _now_et_str() -> str:
    return datetime.now(ET).strftime("%H:%M ET")


async def _guard_skip() -> str | None:
    """Return a reason-string if any STEP 0 guard says skip; None otherwise."""
    import mcp_server

    # 1. AZ quiet window. Orchestrator pre-gates this, but defend against manual runs.
    az_hour = datetime.now(AZ).hour
    if az_hour >= 22 or az_hour < 5:
        return f"AZ quiet window (hour={az_hour})"

    # 2. NYSE market hours.
    status = json.loads(await mcp_server.get_market_status())
    if not status.get("is_open"):
        return f"market closed (next_open={status.get('next_open_et')})"

    # 3. Kill switch.
    ks = json.loads(await mcp_server.get_kill_switch_status())
    if ks.get("global_kill"):
        return "global kill switch active"
    if ks.get("per_agent", {}).get("mike"):
        return "mike kill switch active"

    return None


async def _llm_why_sentence(result: dict) -> str:
    """Ask the local vLLM for a single-sentence summary. Times out hard at 30s."""
    from pipelines.llm_client import make_client

    target_weights = result.get("target_weights") or {}
    contributing = result.get("contributing_views") or {}
    placed = result.get("orders_placed") or []
    cash_weight = float(result.get("cash_weight") or 0.0)
    cash_contributors = result.get("cash_contributors") or []
    skipped = result.get("skipped_views") or []

    top = sorted(target_weights.items(), key=lambda kv: -abs(float(kv[1] or 0)))[:6]
    facts = []
    for sym, w in top:
        contribs = contributing.get(sym) or []
        agent_list = ", ".join(f"{c.get('agent','?')}({float(c.get('weight') or 0):+.2f})" for c in contribs)
        facts.append(f"{sym} {float(w):+.1%}  ({agent_list})")

    placed_lines = []
    for o in placed[:12]:
        r = o.get("result") or {}
        placed_lines.append(f"{o.get('side')} {o.get('qty')} {o.get('symbol')} status={r.get('status')}")

    skip_summary = ""
    if skipped:
        skip_summary = "Skipped (no inverse ETF mapping): " + ", ".join(s.get("symbol", "?") for s in skipped[:5])

    # `/no_think` disables Qwen3's <think> reasoning block — we just want a
    # one-sentence summary, not a chain-of-thought. Keep max_tokens conservative.
    prompt = (
        "/no_think\n"
        "Write ONE sentence (<=200 chars, no preamble, no quotes, no markdown) summarizing why "
        "the desk just took this allocation. Mention the dominant theme — which agents led, "
        "what the desk is leaning into.\n\n"
        f"Top target weights:\n" + "\n".join(facts) + "\n\n"
        f"Orders placed:\n" + ("\n".join(placed_lines) or "(none)") + "\n\n"
        f"Cash reserve: {cash_weight*100:.1f}% (top contributors: {cash_contributors})\n"
        f"{skip_summary}"
    )

    client = make_client(skill_name="mike-allocator-summary")
    resp = await asyncio.wait_for(
        client.client.chat.completions.create(
            model=client.model,
            max_tokens=200,
            messages=[
                {"role": "system", "content": "You are Mike, the desk allocator. Output one sentence only — no preamble, no thinking, no quotes."},
                {"role": "user", "content": prompt},
            ],
        ),
        timeout=30.0,
    )
    text = (resp.choices[0].message.content or "").strip()
    # Strip <think> blocks that some local models emit.
    if "<think>" in text and "</think>" in text:
        text = text.split("</think>", 1)[1].strip()
    text = text.strip('"\' ').replace("\n", " ")
    return text[:220] or "desk rebalanced"


def _template_why(result: dict) -> str:
    placed = result.get("orders_placed") or []
    tw = result.get("target_weights") or {}
    cash = float(result.get("cash_weight") or 0.0)
    if not placed:
        return "no orders placed; conviction stack matched current allocation"
    longs = sum(1 for w in tw.values() if float(w) > 0)
    shorts = sum(1 for w in tw.values() if float(w) < 0)
    bits = []
    if longs:
        bits.append(f"{longs} longs")
    if shorts:
        bits.append(f"{shorts} hedges")
    if cash >= 0.05:
        bits.append(f"{cash*100:.0f}% cash")
    return f"rebalancing into {', '.join(bits) or 'the conviction stack'}"


def _format_telegram(result: dict, n_symbols: int, n_agents: int, why: str) -> str:
    tw = result.get("target_weights") or {}
    cash_weight = float(result.get("cash_weight") or 0.0)
    placed = result.get("orders_placed") or []
    cap_dropped = result.get("cap_dropped") or []
    pending_review = result.get("pending_user_review") or []
    pending_inverse = result.get("pending_inverse_approvals") or []

    filled_or_submitted = sum(
        1 for o in placed
        if (o.get("result") or {}).get("status") in ("submitted", "filled")
    )
    errored = sum(1 for o in placed if "error" in o or (o.get("result") or {}).get("status") == "error")

    top = sorted(tw.items(), key=lambda kv: -abs(float(kv[1] or 0)))[:3]
    top_str = " · ".join(f"{s} {float(w):+.1%}" for s, w in top) if top else "(no targets)"

    extras = []
    if cap_dropped:
        extras.append(f"{len(cap_dropped)} capped")
    if pending_review:
        extras.append(f"{len(pending_review)} awaiting approval")
    if pending_inverse:
        extras.append(f"{len(pending_inverse)} inverse-ETF gated")
    if errored:
        extras.append(f"{errored} errored")
    extras_str = (" · " + " · ".join(extras)) if extras else ""

    cash_str = f" · cash {cash_weight*100:.0f}%" if cash_weight >= 0.05 else ""

    return (
        f"🧭 *Allocator @ {_now_et_str()}*\n"
        f"*Stack:* {n_symbols} sym / {n_agents} agents · top {top_str}{cash_str}\n"
        f"*Placed:* {filled_or_submitted} of {len(placed)}{extras_str}\n"
        f"*Why:* {why}"
    )[:1000]


async def main() -> int:
    start = time.time()
    log.info("runner starting")

    import mcp_server
    from approval.telegram import send_message
    from db import store

    skip_reason = await _guard_skip()
    if skip_reason:
        log.info("skip: %s", skip_reason)
        return 2

    # Sanity check: coverage. Same threshold as the old skill STEP 2(a).
    rows = await store.get_active_convictions()
    symbols = {(r["symbol"] or "").upper() for r in rows if r.get("symbol")}
    agents = {r["agent_name"] for r in rows if r.get("agent_name")}
    n_sym, n_agent = len(symbols), len(agents)
    if n_sym < 3 or n_agent < 2:
        msg = f"🧭 *Allocator @ {_now_et_str()}* — no action. Insufficient views ({n_sym} sym / {n_agent} agents)."
        log.info("insufficient views: %d sym / %d agents", n_sym, n_agent)
        try:
            await send_message(msg, kind="push", meta={"author_agent": "mike"})
        except Exception as exc:
            log.warning("telegram send failed: %s", exc)
        return 0

    log.info("coverage ok: %d sym / %d agents — calling rebalance_desk", n_sym, n_agent)
    result_json = await mcp_server.rebalance_desk(
        caller="mike",
        dry_run=False,
        gross_leverage=1.0,
        max_per_symbol=0.20,
        min_trade_threshold=0.005,
    )
    result = json.loads(result_json)

    if result.get("error"):
        err = result["error"]
        log.error("rebalance_desk error: %s", err)
        try:
            await send_message(f"⚠ allocator: {err}", kind="push", meta={"author_agent": "mike"})
        except Exception:
            pass
        return 1

    decision_id = result.get("decision_id")
    placed = result.get("orders_placed") or []
    cap_dropped = result.get("cap_dropped") or []
    pending_review = result.get("pending_user_review") or []
    pending_inverse = result.get("pending_inverse_approvals") or []
    cash_weight = float(result.get("cash_weight") or 0.0)

    log.info(
        "decision_id=%s nav=$%.2f placed=%d cap_dropped=%d pending_review=%d pending_inverse=%d cash=%.1f%%",
        decision_id, float(result.get("nav") or 0.0), len(placed),
        len(cap_dropped), len(pending_review), len(pending_inverse), cash_weight * 100,
    )
    for o in placed:
        r = o.get("result") or {}
        log.info(
            "  placed %s %s qty=%s status=%s order_id=%s",
            o.get("side"), o.get("symbol"), o.get("qty"),
            r.get("status") or ("error: " + str(o.get("error"))[:80] if o.get("error") else "?"),
            r.get("order_id"),
        )

    try:
        why = await _llm_why_sentence(result)
        log.info("why-sentence (LLM): %s", why)
    except Exception as exc:
        log.warning("LLM why-sentence failed: %s — using template", exc)
        why = _template_why(result)

    body = _format_telegram(result, n_sym, n_agent, why)
    try:
        await send_message(body, kind="push", meta={"author_agent": "mike", "decision_id": decision_id})
    except Exception as exc:
        log.error("telegram send failed: %s", exc)

    log.info("runner done in %.1fs (decision_id=%s)", time.time() - start, decision_id)
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
