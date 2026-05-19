"""Indicator compute + OCAP (out-of-control action plan) rule evaluation.

Called from scripts/stream_bars.py at the end of every 5-min cycle. For each
symbol with a freshly-arrived bar:

  1. Pull the last ~25 bars from local_bars.
  2. Compute rsi_14, sma_20, bb_upper/lower, rolling_std_20, rolling_return_1bar.
  3. UPSERT into local_bar_indicators.
  4. Evaluate every enabled rule in agents/ocap_rules.yaml against the new bar.
  5. If anything fires, enqueue `ocap_triggered_review` jobs (priority=5) for
     every agent that watchlists the symbol — coalesced per (agent, symbol)
     inside the rule-file's coalesce_window_s so storms don't saturate workers.

The OCAP lane preempts routine ticker_review (priority=10) and sector_summary
(priority=20) — see db/schema.py:agent_job. Workers gate kill_switch / quiet
window at pick time, so an OCAP enqueued for a killed agent is skipped cleanly.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from db import store
from tools.analysis.compute_technicals import _bbands, _rsi, _sma

log = logging.getLogger(__name__)

_RULES_PATH = Path("agents/ocap_rules.yaml")
_RULES_CACHE: dict | None = None
_RULES_MTIME: float = 0.0

# Per-(symbol, rule) cooldown. In-process LRU map of (symbol_upper, rule_name)
# → last-fire epoch seconds. Suppresses re-fires of the same rule on the same
# symbol within COOLDOWN_SECONDS, regardless of which agents watchlist it.
# Without this, a single 2σ move on SPY at 14:00 enqueues an ocap_triggered_review
# every 5-min bar for the next ~half hour while the move keeps printing — wasted
# LLM work since the agent already has the picture.
# Cooldown is loaded from rules YAML (`ocap_cooldown_s`, default 1800 = 30min).
# Resets on worker / streamer restart — soft suppression by design.
_FIRED_AT: dict[tuple[str, str], float] = {}


def _load_rules() -> dict:
    """Cached YAML load — refreshes when the file mtime changes."""
    global _RULES_CACHE, _RULES_MTIME
    if not _RULES_PATH.exists():
        return {"coalesce_window_s": 60, "rules": {}}
    mtime = _RULES_PATH.stat().st_mtime
    if _RULES_CACHE is None or mtime != _RULES_MTIME:
        with open(_RULES_PATH, "r", encoding="utf-8") as f:
            _RULES_CACHE = yaml.safe_load(f) or {}
        _RULES_MTIME = mtime
    return _RULES_CACHE


def _compute_indicators(closes: list[float]) -> dict:
    """Indicator snapshot for the bar at closes[-1]. Any field is None when
    there's not enough history yet."""
    sma_20 = _sma(closes, 20)
    rsi_14 = _rsi(closes, 14)
    bb_upper, _bb_mid, bb_lower = _bbands(closes, 20) if len(closes) >= 20 else (None, None, None)

    rolling_return = None
    if len(closes) >= 2 and closes[-2]:
        rolling_return = (closes[-1] - closes[-2]) / closes[-2]

    rolling_std = None
    if len(closes) >= 21:
        # Rolling std over the most recent 20 1-bar returns. 21 closes → 20 returns.
        rets = []
        for i in range(len(closes) - 20, len(closes)):
            if i == 0 or not closes[i - 1]:
                continue
            rets.append((closes[i] - closes[i - 1]) / closes[i - 1])
        if rets:
            mean = sum(rets) / len(rets)
            rolling_std = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5

    return {
        "rsi_14": rsi_14,
        "sma_20": sma_20,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "rolling_std_20": rolling_std,
        "rolling_return_1bar": rolling_return,
    }


def _evaluate_rules(latest_close: float, ind: dict, rules_cfg: dict) -> list[str]:
    """Return list of rule names that fired on this bar."""
    fired: list[str] = []
    rules = (rules_cfg or {}).get("rules") or {}

    cfg = rules.get("rolling_std_breach") or {}
    if cfg.get("enabled") and ind["rolling_std_20"] and ind["rolling_return_1bar"] is not None:
        k = float(cfg.get("k", 2.0))
        if abs(ind["rolling_return_1bar"]) >= k * ind["rolling_std_20"]:
            fired.append("rolling_std_breach")

    cfg = rules.get("bollinger_break") or {}
    if cfg.get("enabled") and ind["bb_upper"] is not None and ind["bb_lower"] is not None:
        if latest_close > ind["bb_upper"] or latest_close < ind["bb_lower"]:
            fired.append("bollinger_break")

    cfg = rules.get("rsi_extreme") or {}
    if cfg.get("enabled") and ind["rsi_14"] is not None:
        if ind["rsi_14"] >= float(cfg.get("rsi_high", 75)) or \
           ind["rsi_14"] <= float(cfg.get("rsi_low", 25)):
            fired.append("rsi_extreme")

    return fired


def _apply_cooldown(symbol: str, fired: list[str], cooldown_s: float) -> list[str]:
    """Filter out rules that fired on this symbol within `cooldown_s` seconds.
    Updates the in-process `_FIRED_AT` map on the surviving entries so the
    cooldown anchors at the most recent fire, not the first.

    Returns the surviving rule names. Suppressed ones get a debug log line.
    """
    if cooldown_s <= 0 or not fired:
        return fired
    now = time.time()
    sym = symbol.upper()
    survived: list[str] = []
    for rule in fired:
        key = (sym, rule)
        last = _FIRED_AT.get(key)
        if last is not None and (now - last) < cooldown_s:
            log.debug("OCAP cooldown suppressed %s/%s (%.0fs since last fire)",
                      sym, rule, now - last)
            continue
        _FIRED_AT[key] = now
        survived.append(rule)
    return survived


async def _agents_watching(symbol: str) -> list[str]:
    """Active agents whose watchlist contains this symbol."""
    pool = await __import__("db.schema", fromlist=["get_pool"]).get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT agent_name FROM agent_watchlist
               WHERE symbol=$1 AND removed_at IS NULL""",
            symbol.upper(),
        )
    return [r["agent_name"] for r in rows]


async def on_bars_arrived(latest_by_symbol: dict[str, dict]) -> dict:
    """Streamer hook. Recomputes indicators for each symbol with a fresh
    bar this cycle, persists them, and fires OCAP enqueues. Returns
    {indicators_written, ocap_enqueued, ocap_coalesced}."""
    if not latest_by_symbol:
        return {"indicators_written": 0, "ocap_enqueued": 0, "ocap_coalesced": 0}

    rules_cfg = _load_rules()
    coalesce_window_s = int(rules_cfg.get("coalesce_window_s", 60))
    cooldown_s = float(rules_cfg.get("ocap_cooldown_s", 1800))  # 30min default

    indicator_rows: list[dict] = []
    fires: list[tuple[str, datetime, list[str]]] = []
    n_suppressed = 0

    for symbol, latest_bar in latest_by_symbol.items():
        bars = await store.get_recent_local_bars(symbol, n=25, interval="5min")
        if not bars:
            continue
        closes = [float(b["close"]) for b in bars if b.get("close") is not None]
        if not closes:
            continue
        ind = _compute_indicators(closes)
        bar_time = bars[-1]["bar_time"]
        indicator_rows.append({
            "symbol": symbol,
            "bar_time": bar_time,
            **ind,
        })
        raw_fired = _evaluate_rules(closes[-1], ind, rules_cfg)
        fired = _apply_cooldown(symbol, raw_fired, cooldown_s)
        n_suppressed += len(raw_fired) - len(fired)
        if fired:
            fires.append((symbol, bar_time, fired))

    n_indicators = await store.upsert_local_bar_indicators(indicator_rows)

    n_enqueued = n_coalesced = 0
    for symbol, bar_time, fired in fires:
        agents = await _agents_watching(symbol)
        for agent in agents:
            payload = {
                "symbol": symbol,
                "bar_time": bar_time.isoformat() if isinstance(bar_time, datetime) else str(bar_time),
                "triggers": fired,
            }
            res = await store.enqueue_job_coalesced(
                agent_name=agent,
                job_type="ocap_triggered_review",
                payload=payload,
                priority=5,
                coalesce_key=f"ocap:{agent}:{symbol}",
                coalesce_window_s=coalesce_window_s,
                triggers_seen=fired,
            )
            if res["action"] == "enqueued":
                n_enqueued += 1
            else:
                n_coalesced += 1

    if fires or n_suppressed:
        log.info("OCAP: %d symbol-rule-fires (%d cooldown-suppressed) → "
                 "%d jobs enqueued, %d coalesced",
                 sum(len(f[2]) for f in fires), n_suppressed, n_enqueued, n_coalesced)
    return {
        "indicators_written": n_indicators,
        "ocap_enqueued": n_enqueued,
        "ocap_coalesced": n_coalesced,
        "ocap_cooldown_suppressed": n_suppressed,
    }
