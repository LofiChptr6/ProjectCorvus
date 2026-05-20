"""MCP server exposing all trading tools to Claude Code.

Start with:  python mcp_server.py
Or via MCP:  configure in .claude/settings.json (see README)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from fastmcp import FastMCP

# Heavy imports go here at module load, BEFORE the asyncio event loop starts —
# deferring them to first tool call has historically caused event-loop deadlocks.
import pandas as _pd
import pandas_market_calendars as _mcal

load_dotenv()

# ── Logging: capture everything to logs/mcp_server.log so crashes leave a trace ─
_log_dir = Path("logs")
_log_dir.mkdir(parents=True, exist_ok=True)
_file_handler = RotatingFileHandler(_log_dir / "mcp_server.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_file_handler])
log = logging.getLogger("mcp_server")

def _excepthook(exc_type, exc_value, exc_tb):
    log.critical("UNCAUGHT: %s", "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
    traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)
sys.excepthook = _excepthook

def _async_excepthook(loop, context):
    log.critical("ASYNC UNCAUGHT: %s", context)
try:
    asyncio.get_event_loop().set_exception_handler(_async_excepthook)
except Exception:
    pass

# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    p = Path("config.yaml")
    if not p.exists():
        print("ERROR: config.yaml not found. Copy config.example.yaml → config.yaml", file=sys.stderr)
        sys.exit(1)
    with open(p) as f:
        return yaml.safe_load(f)


_cfg: dict = {}
_light_initialized = False


# ── Per-tool rate limiting ────────────────────────────────────────────────────
#
# Sliding-window in-process limiter. Defends downstream APIs (Anthropic,
# Telegram, Massive.com, IBKR) and the database from a buggy agent that loops
# forever on the same tool. Lives in the MCP server process — each scheduled
# skill spawns its own server, so limits reset per-skill, which is what we
# want.
import time as _time

_RATE_LIMITS: dict[str, tuple[int, float]] = {
    # tool_name: (max_calls, window_seconds)
    # Sized for one full orchestrator run = 10 sector reviews concurrently +
    # mike-allocator firing 20-50 orders in a single rebalance + heartbeat.
    # Defends against runaway loops without throttling normal operation.
    "place_order":            (100,  60.0),   # allocator: 50 orders/run × headroom
    "rebalance_desk":         (12,   60.0),   # 1/hour scheduled + manual triggers
    "send_telegram_update":   (120, 600.0),   # 12 pings/orchestrator × headroom
    "send_telegram_chart":    (30,  600.0),
    "get_bars":              (600,  60.0),    # 10 agents × ~30 symbols × 2 calls
    "get_quote":            (1000,  60.0),    # allocator + sectors fetching
    "submit_conviction_view": (500, 60.0),    # 10 agents × ~30 symbols × headroom
    "post_to_thread":         (60,  60.0),    # mostly external feeds; user posts are rare
    "get_thread_posts":      (300,  60.0),    # every agent reads on every review
    "search_posts":           (60,  60.0),
    "get_trading_briefing":   (60,  60.0),    # ProjectParrot/Mocha UI panel
}
_rate_calls: dict[str, list[float]] = {}


def _err(message: str, code: str | None = None, **details) -> str:
    """Standard MCP error envelope: returns a JSON string carrying
    `{"error": <message>, "error_code": <code>, "details": {...}}`.

    The `error` key is preserved as the human-readable summary so existing
    callers reading `result["error"]` keep working. The optional
    `error_code` classifies the failure ("validation", "rate_limit",
    "permission", "internal", "not_found", "input", "external") so
    downstream tooling and the LLM can route uniformly without parsing
    free-form English.
    """
    payload: dict = {"error": message}
    if code is not None:
        payload["error_code"] = code
    if details:
        payload["details"] = details
    return json.dumps(payload, default=str)


def _rate_check(tool_name: str) -> tuple[bool, str]:
    """Return (allowed, reason). Records the call when allowed."""
    limit = _RATE_LIMITS.get(tool_name)
    if not limit:
        return True, ""
    max_calls, window = limit
    now = _time.monotonic()
    bucket = _rate_calls.setdefault(tool_name, [])
    cutoff = now - window
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= max_calls:
        return False, f"rate limit: {tool_name} max {max_calls}/{int(window)}s; last {len(bucket)} calls in window"
    bucket.append(now)
    return True, ""


async def _ensure_init_light() -> None:
    """Init config + DB only. Use for tools that don't need IBKR (Telegram, proposals)."""
    global _cfg, _light_initialized
    if _light_initialized:
        return
    _cfg = _load_config()
    from db.schema import init_db
    await init_db()
    _light_initialized = True


async def _ensure_init() -> None:
    """Full init: config + DB + IBKR daemon healthcheck. Required for any trading tool.
    The daemon owns the live ib_async socket; we just verify it's connected so the
    existing Telegram alert still fires on Gateway-down."""
    await _ensure_init_light()
    from ibkr.client import configure
    configure(_cfg)
    try:
        from ibkr import _rpc
        health = await _rpc.get("/healthz")
        if not health.get("connected"):
            raise RuntimeError(f"daemon up but IBKR disconnected (mode={health.get('mode')})")
    except Exception as exc:
        try:
            from approval.telegram import send_message
            await send_message(
                f"⚠️ *IBKR daemon unreachable*\n`{type(exc).__name__}: {exc}`\nTool call aborted. Check ibkr-daemon.service / IB Gateway.",
                kind="push",
                meta={"author_agent": "system", "event": "ibkr_daemon_unreachable"},
            )
        except Exception:
            pass
        raise


# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="ibkr-trading",
    instructions=(
        "Tools for autonomous stock trading via Interactive Brokers. "
        "Always call get_agent_context first to see current positions and account state. "
        "Always call get_quote before placing any order. "
        "The 'reasoning' field on place_order is required — state your thesis, entry criteria, "
        "stop level, and target before placing any trade."
    ),
)


# ── Context ───────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_agent_context(agent_name: str) -> str:
    """
    Get full market context for a named agent: account state, positions, open orders,
    today's fills, P&L, and the agent's strategy. Call this at the start of every routine.

    Args:
        agent_name: Name of the agent (e.g. 'momentum', 'mean_revert', 'macro', 'earnings')
    """
    await _ensure_init()
    from agent.agent_registry import load_agent
    from agent.prompt_builder import build_context_message, build_system_prompt

    agent_cfg = load_agent(agent_name)
    context = await build_context_message(agent_cfg, "context")
    strategy = build_system_prompt(agent_cfg, _cfg)
    return f"=== AGENT STRATEGY ===\n{strategy}\n\n{context}"


# ── Market data ───────────────────────────────────────────────────────────────

@mcp.tool()
async def get_quote(symbol: str) -> str:
    """
    Get real-time quote for a stock: bid, ask, last, volume, day change.
    Call before placing any order to verify current price.

    Args:
        symbol: Stock ticker, e.g. 'AAPL', 'SPY'
    """
    await _ensure_init()
    ok, reason = _validate_symbol(symbol)
    if not ok:
        return _err(f"validation: {reason}", code="validation")
    ok, reason = _rate_check("get_quote")
    if not ok:
        return _err(reason)
    from data.massive_client import get_quote as _get_quote
    return json.dumps(await _get_quote(symbol))


@mcp.tool()
async def get_bars(
    symbol: str,
    bar_size: str,
    duration: str,
    what_to_show: str = "TRADES",
) -> str:
    """
    Get historical OHLCV bars for a symbol.

    Args:
        symbol: Stock ticker
        bar_size: '1 min', '5 mins', '15 mins', '1 hour', '1 day'
        duration: '1 D', '5 D', '1 M', '3 M', '1 Y'
        what_to_show: 'TRADES' (default), 'MIDPOINT', 'BID', 'ASK'
    """
    await _ensure_init()
    ok, reason = _validate_symbol(symbol)
    if not ok:
        return _err(f"validation: {reason}", code="validation")
    ok, reason = _rate_check("get_bars")
    if not ok:
        return _err(reason)
    from data.massive_client import get_bars as _get_bars
    return json.dumps(await _get_bars(symbol, bar_size, duration, what_to_show))


@mcp.tool()
async def run_scanner(
    scan_type: str,
    num_rows: int = 20,
    above_price: Optional[float] = None,
    below_price: Optional[float] = None,
    above_volume: Optional[int] = None,
) -> str:
    """
    Run an IBKR market scanner to find stocks matching criteria.

    Args:
        scan_type: 'TOP_PERC_GAIN', 'TOP_PERC_LOSE', 'MOST_ACTIVE', 'HOT_BY_VOLUME',
                   'TOP_PRICE_RANGE', 'HIGH_VS_13W_HL', 'LOW_VS_13W_HL'
        num_rows: Max results (default 20)
        above_price: Filter minimum price
        below_price: Filter maximum price
        above_volume: Filter minimum volume
    """
    await _ensure_init()
    from ibkr.market_data import run_scanner as _run_scanner
    return json.dumps(await _run_scanner(scan_type, num_rows, above_price, below_price, above_volume))


@mcp.tool()
async def get_news(symbol: Optional[str] = None, max_items: int = 10) -> str:
    """
    Fetch recent news headlines for a symbol or the general market.

    Args:
        symbol: Stock ticker. Omit for market-wide news.
        max_items: Max headlines to return (default 10)
    """
    await _ensure_init()
    from data.massive_client import get_news as _get_news
    return json.dumps(await _get_news(symbol, max_items))


@mcp.tool()
async def semantic_news_recall(
    query: str,
    top_k: int = 10,
    half_life_hours: float = 24.0,
    agent_name: Optional[str] = None,
    symbols: Optional[list[str]] = None,
    max_age_days: int = 30,
) -> str:
    """
    Search recent news by SEMANTIC SIMILARITY to a free-text query, with
    exponential time-decay weighting. Use this when you need older-but-still-
    relevant articles that wouldn't appear in get_news() (which only returns
    the latest headlines per ticker).

    Score formula:
        score = cosine_similarity(query, article) * exp(-ln(2) * hours_old / half_life_hours)

    A half_life_hours of 24 means an article from 24h ago at sim=1.0 ties
    one from 30 min ago at sim=0.5. Use 168 (=7d) for weekly themes,
    720 (=30d) for month-scale narratives.

    Args:
        query: Free-text describing what you want — e.g.
               "OPEC production cuts and crude oversupply" or
               "EV demand softening in China"
        top_k: How many results to return (default 10).
        half_life_hours: Time-decay constant. 24=hot, 168=weekly, 720=monthly.
        agent_name: For logging only; does not filter results.
        symbols: Optional whitelist of tickers (e.g. ["XLE","XOM","CVX"]).
        max_age_days: Hard cutoff — older articles excluded (default 30).

    Returns:
        JSON list of {id, symbol, headline, body, url, published_at, provider,
        sentiment, sim, hours_old, score} sorted by score desc. Empty list
        if no semantic provider is configured or pgvector isn't installed —
        check the logs for the cause.
    """
    await _ensure_init()
    from db.semantic import semantic_news_recall as _recall
    rows = await _recall(
        query_text=query,
        agent_name=agent_name,
        top_k=top_k,
        half_life_hours=half_life_hours,
        symbol_filter=symbols,
        max_age_days=max_age_days,
    )
    return json.dumps(rows, default=str)


# ── Account ───────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_positions() -> str:
    """
    Get all open positions: symbol, quantity, avg cost, unrealized P&L.
    Check this before buying or selling to avoid unwanted exposure.
    """
    await _ensure_init()
    from ibkr.account import get_positions as _get_positions
    return json.dumps({"positions": await _get_positions()})


@mcp.tool()
async def get_balances() -> str:
    """
    Get account balances: NAV, cash, buying power, today's realized + unrealized
    P&L, and combined total. Combined = realized + unrealized — what the desk
    is actually up/down right now (open positions included).
    """
    await _ensure_init()
    from ibkr.account import get_account_summary
    summary = await get_account_summary()
    realized = float(summary.get("realized_pnl_today") or 0.0)
    unrealized = float(summary.get("unrealized_pnl") or 0.0)
    summary["combined_pnl_today"] = realized + unrealized
    return json.dumps(summary)


@mcp.tool()
async def get_open_orders() -> str:
    """
    Get all working orders (not yet filled or cancelled), including partial fill status.
    Call after place_order to confirm submission.
    """
    await _ensure_init()
    from ibkr.account import get_open_orders as _get_open_orders
    return json.dumps({"orders": await _get_open_orders()})


# ── Execution ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def place_order(
    agent_name: str,
    symbol: str,
    action: str,
    quantity: float,
    order_type: str,
    reasoning: str,
    limit_price: Optional[float] = None,
    stop_price: Optional[float] = None,
    dry_run: bool = False,
) -> str:
    """
    Place a stock order. Goes through risk checks, then Telegram approval for large orders.
    Returns 'submitted', 'blocked' (with reason), or 'approval_rejected'.

    Args:
        agent_name: Which agent is placing this order (e.g. 'momentum')
        symbol: Stock ticker
        action: 'BUY' or 'SELL'
        quantity: Number of shares (positive)
        order_type: 'MKT', 'LMT', or 'STP'
        reasoning: REQUIRED. Why are you placing this order? State thesis, stop, target.
        limit_price: Required for LMT orders
        stop_price: Required for STP orders
        dry_run: If True, validates but does not submit to IBKR
    """
    await _ensure_init()

    # Stage-3 gate: under sector-shard architecture, only mike (the allocator)
    # may place real orders. Other agents publish conviction views and rely on
    # mike-allocator to translate them into trades. Defense-in-depth on top of
    # the prompt-level instructions in each *-review.md.
    if agent_name != "mike":
        return json.dumps({
            "status": "blocked",
            "reason": (
                f"agent '{agent_name}' is not authorized to place orders directly. "
                "Sector agents publish conviction via submit_conviction_view; "
                "mike-allocator runs the trades."
            ),
            "check": "sector_shard_gate",
        })

    ok, reason = _validate_symbol(symbol)
    if not ok:
        return json.dumps({"status": "blocked", "reason": reason, "check": "input_validation"})
    ok, reason = _validate_rationale(reasoning, max_len=1024)
    if not ok:
        return json.dumps({"status": "blocked", "reason": reason, "check": "input_validation"})
    if action not in ("BUY", "SELL"):
        return json.dumps({"status": "blocked", "reason": f"invalid action: {action!r}", "check": "input_validation"})
    if order_type not in ("MKT", "LMT", "STP"):
        return json.dumps({"status": "blocked", "reason": f"invalid order_type: {order_type!r}", "check": "input_validation"})
    ok, reason = _rate_check("place_order")
    if not ok:
        return json.dumps({"status": "blocked", "reason": reason, "check": "rate_limit"})

    if dry_run:
        return json.dumps({
            "status": "dry_run",
            "would_place": {
                "agent_name": agent_name, "symbol": symbol, "action": action,
                "quantity": quantity, "order_type": order_type,
                "limit_price": limit_price, "reasoning": reasoning,
            },
        })

    from ibkr.account import get_account_summary, get_positions
    from risk.models import AccountState, OrderRequest
    from risk.guardrails import check as risk_check

    # Fetch a quote for market orders so the min-quantity gate can compare
    # against the live price. Limit/stop orders carry their own price already;
    # only call out for MKT to avoid the round-trip.
    current_mark: Optional[float] = None
    if order_type == "MKT":
        try:
            from data.massive_client import get_quote as _get_quote
            q = await _get_quote(symbol)
            current_mark = float(q.get("last") or q.get("close") or 0.0) or None
        except Exception:
            current_mark = None

    order = OrderRequest(
        symbol=symbol, action=action, quantity=quantity, order_type=order_type,
        limit_price=limit_price, stop_price=stop_price, reasoning=reasoning,
        agent_name=agent_name, current_mark=current_mark,
    )
    summary = await get_account_summary()
    positions = await get_positions()
    account = AccountState(
        nav=summary.get("nav", 0),
        cash=summary.get("cash", 0),
        buying_power=summary.get("buying_power", 0),
        realized_pnl_today=summary.get("realized_pnl_today", 0),
        positions=positions,
    )

    risk_result = await risk_check(order, account, _cfg)
    if not risk_result.allowed:
        # Sub-10-share gate: when the only blocker is min-quantity on an
        # expensive ticker, fall through to the synchronous Telegram override
        # rather than rejecting outright.
        if risk_result.needs_telegram_approval:
            from approval.workflow import request_approval
            est_notional = quantity * (order.effective_price or 0.0)
            override = await request_approval(order, est_notional, None, _cfg)
            if not override.approved:
                return json.dumps({
                    "status": "approval_rejected",
                    "reason": f"sub-{int(_cfg.get('risk', {}).get('min_shares_per_order', 10))}-share override declined: {override.reason}",
                    "check": "min_quantity",
                })
            # Fall through to standard large-notional approval gate below.
        else:
            return json.dumps({"status": "blocked", "reason": risk_result.reason, "check": risk_result.check_name})

    # Human approval for large orders
    price = limit_price or stop_price or current_mark or 0
    notional = quantity * price
    approval_cfg = _cfg.get("approval", {})
    if approval_cfg.get("enabled", True) and notional >= approval_cfg.get("threshold_usd", 5000):
        from approval.workflow import request_approval
        approval = await request_approval(order, notional, None, _cfg)
        if not approval.approved:
            return json.dumps({"status": "approval_rejected", "reason": approval.reason})

    from ibkr.orders import place_order as ibkr_place
    result = await ibkr_place(
        symbol=symbol, action=action, quantity=quantity, order_type=order_type,
        limit_price=limit_price, stop_price=stop_price,
        agent_name=agent_name, reasoning=reasoning,
    )
    return json.dumps(result)


@mcp.tool()
async def cancel_order(order_id: int, reasoning: str) -> str:
    """
    Cancel an open order by its IBKR order ID.

    Args:
        order_id: IBKR order ID from get_open_orders
        reasoning: Why you are cancelling
    """
    await _ensure_init()
    from ibkr.orders import cancel_order as _cancel
    return json.dumps(await _cancel(order_id))


@mcp.tool()
async def modify_order(
    order_id: int,
    reasoning: str,
    new_limit_price: Optional[float] = None,
    new_quantity: Optional[float] = None,
) -> str:
    """
    Modify the price or quantity of a working limit order.

    Args:
        order_id: IBKR order ID
        reasoning: Why you are modifying
        new_limit_price: New limit price (omit to keep current)
        new_quantity: New quantity (omit to keep current)
    """
    await _ensure_init()
    from ibkr.orders import modify_order as _modify
    return json.dumps(await _modify(order_id, new_limit_price, new_quantity))


# ── Analysis ──────────────────────────────────────────────────────────────────

@mcp.tool()
async def compute_technicals(symbol: str, indicators: list[str]) -> str:
    """
    Compute technical indicators on daily bar data for a symbol.

    Args:
        symbol: Stock ticker
        indicators: List from: 'SMA_20', 'SMA_50', 'SMA_200', 'EMA_9', 'EMA_21',
                    'RSI_14', 'VWAP', 'ATR_14', 'BBANDS_20'
    """
    await _ensure_init()
    ok, reason = _validate_symbol(symbol)
    if not ok:
        return _err(f"validation: {reason}", code="validation")
    from tools.analysis.compute_technicals import execute
    return await execute(symbol=symbol, indicators=indicators)


@mcp.tool()
async def get_agent_pnl_windows(agent_name: Optional[str] = None) -> str:
    """
    Per-agent P&L over rolling windows (1d, week-to-date, 1 month, 3 month)
    computed from `agent_state.total_pnl` deltas — no IBKR call. Each window:
        pnl_usd = total_pnl(now) − total_pnl(window_start)
    where window_start resolves to the latest snapshot at-or-before the
    calendar window start. Windows with no snapshot return None.

    `total_pnl` is cumulative since inception (realized + unrealized), so
    settlement noise and cash-attribution artifacts don't appear in the
    delta — see DESK_POLICY §0/§7. Reflects the agent_state table as-of
    its most recent UPSERT (hourly cron + every mike rebalance).

    Args:
        agent_name: Filter to one agent. Omit for the full per-agent table.
    """
    await _ensure_init_light()
    from reporting.agent_pnl import get_pnl_windows
    return json.dumps(await get_pnl_windows(agent_name=agent_name), default=str)


@mcp.tool()
async def get_pnl_summary(agent_name: Optional[str] = None) -> str:
    """
    COMBINED (realized + unrealized) P&L by agent — the primary leaderboard.
    Reads the latest `agent_state` snapshot per agent (no IBKR call). Numbers
    are CUMULATIVE since inception, not windowed; for windowed P&L use
    `get_agent_pnl_windows`.

    Each row: {agent_name, realized_pnl, unrealized_pnl, total_pnl,
               open_cost, open_market_value, n_positions, snapshot_at}.
    Desk totals roll up underneath in `totals`.

    Args:
        agent_name: Filter by agent (omit for all agents).
    """
    await _ensure_init_light()
    from reporting.agent_pnl import get_pnl_combined
    combined = await get_pnl_combined(agent_name=agent_name)
    totals = {
        "total_pnl": combined["desk"]["combined_total"],
        "realized_pnl": combined["desk"]["realized_total"],
        "unrealized_pnl": combined["desk"]["unrealized_total"],
        "n_agents": combined["desk"]["n_agents"],
        "snapshot_at": combined["desk"]["snapshot_at"],
    }
    return json.dumps({"by_agent": combined["rows"], "totals": totals})


@mcp.tool()
async def get_my_pnl(agent_name: str) -> str:
    """
    COMBINED (realized + unrealized) P&L for your agent from the latest
    `agent_state` snapshot. Use in evening reviews. No IBKR call.

    Returns: {realized_pnl, unrealized_pnl, total_pnl, n_positions,
              snapshot_at, desk_total_pnl}
    """
    await _ensure_init_light()
    from reporting.agent_pnl import get_pnl_combined
    combined = await get_pnl_combined(agent_name=agent_name)
    agent_row = next((r for r in combined["rows"] if r["agent_name"] == agent_name), {
        "agent_name": agent_name, "realized_pnl": 0.0,
        "unrealized_pnl": 0.0, "total_pnl": 0.0,
        "open_cost": 0.0, "open_market_value": 0.0,
        "n_positions": 0, "snapshot_at": None,
    })
    return json.dumps({
        "agent_name": agent_name,
        "realized_pnl": agent_row.get("realized_pnl", 0.0),
        "unrealized_pnl": agent_row.get("unrealized_pnl", 0.0),
        "total_pnl": agent_row.get("total_pnl", 0.0),
        "open_cost": agent_row.get("open_cost", 0.0),
        "open_market_value": agent_row.get("open_market_value", 0.0),
        "n_positions": agent_row.get("n_positions", 0),
        "snapshot_at": agent_row.get("snapshot_at"),
        "desk_total_pnl": combined["desk"]["combined_total"],
        "note": "lifetime cumulative; deltas via get_agent_pnl_windows",
    })


@mcp.tool()
async def get_desk_policy() -> str:
    """
    Return the canonical desk-wide operating rules all agents must follow.
    Call this at the start of any review session to internalize current policy.
    """
    policy_path = Path(__file__).parent / "DESK_POLICY.md"
    try:
        return json.dumps({"policy": policy_path.read_text()})
    except FileNotFoundError:
        return json.dumps({"policy": "(DESK_POLICY.md not found)"})


@mcp.tool()
async def get_trade_blotter(
    symbol: Optional[str] = None,
    date: Optional[str] = None,
    agent_name: Optional[str] = None,
    limit: int = 50,
) -> str:
    """
    Get fill history (executed trades).

    Args:
        symbol: Filter by symbol (optional)
        date: Filter by date YYYY-MM-DD (default: today)
        agent_name: Filter by agent (optional)
        limit: Max rows (default 50)
    """
    await _ensure_init_light()
    from datetime import date as dt_date
    import db.store as store
    if date is None:
        date = dt_date.today().isoformat()
    fills = await store.get_fills(symbol=symbol, date=date, agent_name=agent_name, limit=limit)
    return json.dumps({"fills": fills, "count": len(fills)})


# ── Risk / System ─────────────────────────────────────────────────────────────

@mcp.tool()
async def get_kill_switch_status() -> str:
    """Check whether the kill switch is active (globally or per agent).

    Semantics (post-2026-05 refactor):
      - `global_kill`: when True, mike-allocator skips entirely (no rebalance,
        no orders) and the order layer fails closed. Workers still run
        sector reviews / OCAP-triggered reviews — agents publish convictions
        as usual; mike just doesn't act on them.
      - `per_agent` (pipeline sectors + mike): when True for sector X, mike
        filters X's convictions out at the allocator load step
        (`db.store.get_active_convictions`). X still analyzes and writes
        convictions; X just doesn't influence orders. When True for `mike`,
        the allocator itself skips.
      - `non_pipeline_per_agent` (e.g. cassidy): kill state recorded but
        inert — these agents don't participate in conviction publishing,
        so their kill flag has no allocator-facing effect. Surfaced
        separately so the UI doesn't suggest a control that does nothing.
    """
    await _ensure_init_light()
    import db.store as store
    from meta_agent.queue_primer import PIPELINE_SECTORS
    global_killed = await store.is_killed()
    from agent.agent_registry import list_agents
    agents = list_agents(enabled_only=False)
    # Pipeline = the 11 sector agents that publish convictions + mike (the
    # allocator). For these, per-agent kill changes desk behavior.
    pipeline_set = set(PIPELINE_SECTORS) | {"mike"}
    per_agent: dict[str, bool] = {}
    non_pipeline_per_agent: dict[str, bool] = {}
    for a in agents:
        name = a["name"]
        is_k = await store.is_killed(agent_name=name)
        if name in pipeline_set:
            per_agent[name] = is_k
        else:
            non_pipeline_per_agent[name] = is_k
    return json.dumps({
        "global_kill": global_killed,
        "per_agent": per_agent,
        "non_pipeline_per_agent": non_pipeline_per_agent,
    })


@mcp.tool()
async def get_kill_switch_history(hours: int = 24) -> str:
    """Recent kill_switch activations (and deactivations) over the last N
    hours. Use in evening / post-mortem reviews to surface "why was atlas
    halted at 14:23 today" — the current-state tool `get_kill_switch_status`
    only shows the latest per-agent state, not the trail.

    Returns: {"window_hours": N, "events": [
      {id, agent_name, is_active, activated_at, activated_by, reason,
       deactivated_at}, ...
    ]} sorted by id DESC.
    """
    await _ensure_init_light()
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, agent_name, is_active, activated_at, activated_by,
                      reason, deactivated_at
               FROM kill_switch
               WHERE COALESCE(activated_at::timestamptz,
                              deactivated_at::timestamptz) >
                     NOW() - ($1 || ' hours')::interval
               ORDER BY id DESC
               LIMIT 100""",
            str(int(hours)),
        )
    return json.dumps({
        "window_hours": int(hours),
        "events": [dict(r) for r in rows],
    }, default=str)


@mcp.tool()
async def activate_kill_switch(reason: str, agent_name: Optional[str] = None) -> str:
    """
    Activate the kill switch.

    Effect (post-2026-05 refactor): kill freezes the order flow, NOT the
    analysis pipeline. Agents continue to run sector reviews and OCAP-fired
    reviews; their convictions still write to `agent_conviction`. Mike's
    allocator is what stops:
      - `agent_name=None` (global) — mike-allocator's `_guard_skip` returns
        early; no rebalance, no orders.
      - `agent_name="mike"` — same effect; mike's own per-agent kill blocks
        the allocator.
      - `agent_name=<sector>` (atlas / energy / fab / ...) — that sector's
        convictions get filtered out at mike's load step; sector's votes
        are muted while it still analyzes.
      - `agent_name=<non-pipeline>` (e.g. cassidy) — recorded but inert
        (no allocator-facing effect).
    Order layer (`risk/checks/kill_switch.py`) re-checks immediately before
    IBKR submit, so a kill that fires during the approval window still
    blocks the order.

    Args:
        reason: Why you are halting
        agent_name: Specific agent to halt, or omit for global halt
    """
    await _ensure_init_light()
    import db.store as store
    await store.set_kill_switch(active=True, agent_name=agent_name, activated_by="claude_code", reason=reason)
    scope = f"agent={agent_name}" if agent_name else "GLOBAL"
    try:
        from approval.telegram import send_message
        await send_message(
            f"🛑 *Kill switch activated* ({scope})\nReason: {reason}",
            kind="push",
            meta={"author_agent": "system", "event": "kill_switch_activated", "scope": scope},
        )
    except Exception:
        pass
    return json.dumps({"status": "activated", "scope": scope, "reason": reason})


@mcp.tool()
async def get_agent_list() -> str:
    """List all configured agents with their allocation and enabled status."""
    await _ensure_init_light()
    from meta_agent.allocation_manager import get_all_allocations
    allocs = await get_all_allocations()
    return json.dumps({"agents": allocs})


@mcp.tool()
async def get_market_status() -> str:
    """Authoritative NYSE market-hours check via pandas_market_calendars.
    Returns whether the market is open right now, today's session bounds, whether
    it is a half-day, and the next open/close in US/Eastern. Use this to decide
    trading vs. off-hours mode — do NOT rely on wall-clock weekday logic, which
    misses holidays and half-days."""
    # pandas / pandas_market_calendars are imported at module load; see top of file.
    pd = _pd
    mcal = _mcal

    nyse = mcal.get_calendar("NYSE")
    now_et = pd.Timestamp.now(tz="America/New_York")
    # Look at a 14-day window so we can always find the next session.
    sched = nyse.schedule(
        start_date=(now_et - pd.Timedelta(days=1)).normalize(),
        end_date=(now_et + pd.Timedelta(days=14)).normalize(),
    )
    is_open = False
    if not sched.empty:
        try:
            is_open = bool(nyse.open_at_time(sched, now_et))
        except ValueError:
            is_open = False

    today_key = now_et.normalize().tz_localize(None)
    today_row = sched.loc[today_key] if today_key in sched.index else None
    today_session = None
    is_half_day = False
    if today_row is not None:
        open_et = today_row["market_open"].tz_convert("America/New_York")
        close_et = today_row["market_close"].tz_convert("America/New_York")
        today_session = {
            "open_et":  open_et.isoformat(),
            "close_et": close_et.isoformat(),
        }
        # Regular NYSE close is 16:00 ET; anything earlier is a half-day.
        is_half_day = close_et.hour < 16

    # Next open after now
    future = sched[sched["market_open"] > now_et]
    next_open = future.iloc[0]["market_open"].tz_convert("America/New_York").isoformat() if not future.empty else None

    return json.dumps({
        "now_et": now_et.isoformat(),
        "is_open": is_open,
        "mode": "trading" if is_open else "off_hours",
        "today_session": today_session,
        "is_half_day": is_half_day,
        "next_open_et": next_open,
    })


@mcp.tool()
async def get_queue_health() -> str:
    """Snapshot of the LLM-task queue. Reports counts by status, the oldest
    queued job's age, recent throughput, and per-job-type breakdown so the
    operator (or a sibling agent) can detect backlog before convictions
    silently expire.

    Returns: {
      "by_status": {"queued": N, "running": N, ...},
      "oldest_queued_age_s": float | null,
      "queued_by_job_type": {"ticker_review": N, "sector_summary": N, ...},
      "done_last_hour": int,
      "failed_last_hour": int,
      "skipped_last_hour": int,
      "avg_duration_ms_last_hour": float | null,
      "worker_ids_active": [str, ...]
    }

    Use when:
      - Convictions look stale across multiple agents (check if queue is
        backed up).
      - Investigating why an OCAP-triggered review didn't land promptly.
      - Capacity-planning: deciding whether to add more queue workers.
    """
    await _ensure_init_light()
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        by_status_rows = await conn.fetch(
            "SELECT status, COUNT(*) AS n FROM agent_job GROUP BY status"
        )
        oldest_queued = await conn.fetchval(
            """SELECT EXTRACT(EPOCH FROM (NOW() - MIN(enqueued_at)))
               FROM agent_job WHERE status='queued'"""
        )
        queued_by_type = await conn.fetch(
            """SELECT job_type, COUNT(*) AS n FROM agent_job
               WHERE status='queued' GROUP BY job_type"""
        )
        recent_window = await conn.fetch(
            """SELECT status, COUNT(*) AS n,
                      AVG(EXTRACT(EPOCH FROM (finished_at - started_at)) * 1000) AS avg_ms
               FROM agent_job
               WHERE finished_at > NOW() - INTERVAL '1 hour'
               GROUP BY status"""
        )
        active_workers = await conn.fetch(
            """SELECT DISTINCT worker_id FROM agent_job
               WHERE status='running' AND worker_id IS NOT NULL"""
        )

    recent = {r["status"]: {"n": int(r["n"]),
                            "avg_ms": float(r["avg_ms"]) if r["avg_ms"] else None}
              for r in recent_window}
    return json.dumps({
        "by_status": {r["status"]: int(r["n"]) for r in by_status_rows},
        "oldest_queued_age_s": float(oldest_queued) if oldest_queued else None,
        "queued_by_job_type": {r["job_type"]: int(r["n"]) for r in queued_by_type},
        "done_last_hour": recent.get("done", {}).get("n", 0),
        "failed_last_hour": recent.get("failed", {}).get("n", 0),
        "skipped_last_hour": recent.get("skipped", {}).get("n", 0),
        "avg_duration_ms_last_hour": recent.get("done", {}).get("avg_ms"),
        "worker_ids_active": [r["worker_id"] for r in active_workers],
    })


@mcp.tool()
async def prime_sector_queues() -> str:
    """Manually fire phase 1a of the hourly orchestrator — prime the LLM-task
    queue with one sector_summary + N ticker_review job per sector agent.
    Idempotent within the 1-hour coalesce window, so repeat calls are no-ops
    on jobs already queued/running.

    Same code path as the hourly cron (`meta_agent.queue_primer`). Workers
    still gate execution on kill_switch and the AZ quiet window — priming
    does NOT bypass any safety control. Use when you want the sector agents
    to react to fresh data without waiting for the next hourly tick (e.g.
    after a market event, after lifting kill_switch, after a watchlist
    change).

    Returns: {
      "total_enqueued": int,
      "total_coalesced": int,
      "failed_agents": [agent, ...],
      "duration_ms": int,
      "per_agent": [
        {"agent": str, "enqueued": int, "coalesced": int, "watchlist_size": int}
        OR {"agent": str, "error": str},
        ...
      ],
    }
    """
    await _ensure_init_light()
    from meta_agent.queue_primer import prime_all_agent_queues
    return json.dumps(await prime_all_agent_queues(), default=str)


@mcp.tool()
async def get_tool_error_summary(
    agent_name: Optional[str] = None,
    since_hours: int = 24,
    min_errors: int = 1,
) -> str:
    """Aggregated tool-call failures over the last N hours, grouped by
    (agent_name, tool_name). Surfaces systemic tool problems — e.g. "energy
    has called get_news 20 times and 18 failed" — which would otherwise be
    invisible without grepping per-session logs.

    Returns: {"window_hours": N, "rows": [
      {agent_name, tool_name, total_calls, error_calls, error_rate,
       last_error_at, last_error_msg (truncated 300 chars)}, ...
    ]}. Ordered by error_calls desc.

    Use when:
      - Cassidy is writing the evening risk review — folds into the
        per-agent tool-failure section.
      - Investigating why an agent's reviews keep crashing or returning
        partial output.
      - Catching MCP-side regressions: a tool that started failing across
        many agents at once is a server-side problem, not an agent one.
    """
    await _ensure_init_light()
    from obs.queries import _tool_error_summary
    rows = await _tool_error_summary(agent_name, since_hours, min_errors)
    return json.dumps({
        "window_hours": int(since_hours),
        "rows": rows,
    }, default=str)


@mcp.tool()
async def get_streamer_health() -> str:
    """Health of the local market-data caches the streamer + daily ingestor
    populate. Surfaces ingest lag so the operator notices if a daemon stalled.

    Returns: {
      "local_bars": {
         "total_rows", "n_symbols", "newest_bar_time", "oldest_bar_time",
         "newest_ingest_at", "newest_ingest_age_s"
      },
      "local_bars_daily": {
         "total_rows", "n_symbols", "newest_bar_date", "newest_ingest_at",
         "newest_ingest_age_s"
      }
    }

    `newest_ingest_age_s` is the headline number: during RTH this should be
    well under 300 (one streamer cycle). Outside RTH it grows naturally.
    For local_bars_daily, expect ≤ 24h after each post-close ingest fire.
    """
    await _ensure_init_light()
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        intraday = await conn.fetchrow(
            """SELECT COUNT(*) AS total_rows,
                      COUNT(DISTINCT symbol) AS n_symbols,
                      MAX(bar_time) AS newest_bar_time,
                      MIN(bar_time) AS oldest_bar_time,
                      MAX(ingested_at) AS newest_ingest_at,
                      EXTRACT(EPOCH FROM (NOW() - MAX(ingested_at))) AS newest_ingest_age_s
               FROM local_bars"""
        )
        daily = await conn.fetchrow(
            """SELECT COUNT(*) AS total_rows,
                      COUNT(DISTINCT symbol) AS n_symbols,
                      MAX(bar_date) AS newest_bar_date,
                      MAX(ingested_at) AS newest_ingest_at,
                      EXTRACT(EPOCH FROM (NOW() - MAX(ingested_at))) AS newest_ingest_age_s
               FROM local_bars_daily"""
        )
    return json.dumps({
        "local_bars": {
            "total_rows": int(intraday["total_rows"] or 0),
            "n_symbols": int(intraday["n_symbols"] or 0),
            "newest_bar_time": intraday["newest_bar_time"],
            "oldest_bar_time": intraday["oldest_bar_time"],
            "newest_ingest_at": intraday["newest_ingest_at"],
            "newest_ingest_age_s": float(intraday["newest_ingest_age_s"])
                if intraday["newest_ingest_age_s"] is not None else None,
        },
        "local_bars_daily": {
            "total_rows": int(daily["total_rows"] or 0),
            "n_symbols": int(daily["n_symbols"] or 0),
            "newest_bar_date": daily["newest_bar_date"],
            "newest_ingest_at": daily["newest_ingest_at"],
            "newest_ingest_age_s": float(daily["newest_ingest_age_s"])
                if daily["newest_ingest_age_s"] is not None else None,
        },
    }, default=str)


# ── Telegram / proposals ──────────────────────────────────────────────────────

@mcp.tool()
async def send_telegram_update(text: str, author_agent: Optional[str] = None) -> str:
    """
    Send a plain status message to the user via Telegram.
    Use for the hourly summary ping. Does NOT require a reply.

    Args:
        text: Markdown-formatted message body.
        author_agent: Optional agent identifier ('mike', 'atlas', 'cassidy', …)
                      for the telegram_message audit log. Defaults to 'system'.
    """
    await _ensure_init_light()
    ok, reason = _rate_check("send_telegram_update")
    if not ok:
        return json.dumps({"sent": False, "error": reason})
    from approval.telegram import send_message
    result = await send_message(
        text,
        kind="push",
        meta={"author_agent": author_agent or "system"},
    )
    return json.dumps({"sent": result is not None})


@mcp.tool()
async def propose_strategic_change(title: str, details: str) -> str:
    """
    Create a pending strategic-change proposal and send the initial Telegram ping.
    The proposal persists to disk and will be auto-nudged every 5 minutes until
    the user replies "y" or "n" in Telegram.

    Use this for: reallocating capital, enabling/disabling an agent, changing
    risk limits, shifting an agent's thesis, creating a new agent (cap 10 total),
    or modifying strategy code.

    Args:
        title: Short title (e.g. 'Disable mean_revert agent').
        details: Full rationale — why, impact, what changes if approved.
    """
    await _ensure_init_light()
    from approval import proposals
    p = await proposals.create(title=title, details=details)
    return json.dumps({"proposal_id": p["id"], "short_id": p["id"][:8], "status": p["status"]})


@mcp.tool()
async def list_pending_proposals() -> str:
    """List all pending (unresolved) strategic-change proposals."""
    await _ensure_init_light()
    from approval import proposals
    return json.dumps({"pending": proposals.list_pending()})


# ── Mike analysis (director) ──────────────────────────────────────────────────

# Market-anchored date — single source of truth for "today" across the whole system.
def _market_date() -> str:
    """Return today's date in America/New_York, ISO format."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        # Fallback for environments missing tzdata
        from datetime import date as _d
        return _d.today().isoformat()


def _resolve_date(date: str) -> str:
    return _market_date() if date == "today" else date


def _parse_window(window: str) -> tuple[str, str | None]:
    """'today'/'week'/'month', Nd shorthand (e.g. '5d','30d'), or 'YYYY-MM-DD/YYYY-MM-DD' → (since_iso, until_iso). until=None means up to now."""
    from datetime import date, timedelta
    today = date.today()
    if window == "today":
        return today.isoformat() + "T00:00:00+00:00", None
    if window == "week":
        return (today - timedelta(days=7)).isoformat() + "T00:00:00+00:00", None
    if window == "month":
        return (today - timedelta(days=30)).isoformat() + "T00:00:00+00:00", None
    if window.endswith("d") and window[:-1].isdigit():
        return (today - timedelta(days=int(window[:-1]))).isoformat() + "T00:00:00+00:00", None
    if "/" in window:
        a, b = window.split("/", 1)
        return a + "T00:00:00+00:00", b + "T23:59:59+00:00"
    return window + "T00:00:00+00:00", window + "T23:59:59+00:00"


# Per-date async lock so morning + midday writes cannot interleave.
_MIKE_LOCKS: dict[str, asyncio.Lock] = {}

def _mike_lock(date_str: str) -> asyncio.Lock:
    lock = _MIKE_LOCKS.get(date_str)
    if lock is None:
        lock = asyncio.Lock()
        _MIKE_LOCKS[date_str] = lock
    return lock


@mcp.tool()
async def write_mike_analysis(
    analysis: str,
    date: str = "today",
    regime: Optional[str] = None,
    risk_tone: Optional[str] = None,
    atlas_guidance: Optional[str] = None,
    fab_guidance: Optional[str] = None,
    fabless_guidance: Optional[str] = None,
    iron_guidance: Optional[str] = None,
    maya_guidance: Optional[str] = None,
    rex_guidance: Optional[str] = None,
    trump_guidance: Optional[str] = None,
    vera_guidance: Optional[str] = None,
    volt_guidance: Optional[str] = None,
    energy_guidance: Optional[str] = None,
    commodity_guidance: Optional[str] = None,
    sector_rotation: Optional[str] = None,
    overnight_notes: Optional[str] = None,
) -> str:
    """
    Persist Mike's market analysis for the given date. Writes TWO files:
    - YYYY-MM-DD.txt — full free-form analysis (appended, with UTC separator)
    - YYYY-MM-DD.json — structured per-agent sections (overwritten each call)

    Both morning and midday calls update the JSON. Sector agents read the JSON
    per-agent via get_mike_analysis(agent_name=...) so they only see their own
    guidance.

    Args:
        analysis: Full analysis text (markdown). Always required — this is the
            human-readable record.
        date: 'today' (default, market-anchored to America/New_York) or 'YYYY-MM-DD'.
        regime: One of 'BULLISH', 'BEARISH', 'NEUTRAL', 'TRANSITIONAL'. Required for
            the first write of the day; optional for updates.
        risk_tone: One-sentence summary of today's risk appetite.
        <agent>_guidance: Per-agent directives. The 11 sector agents are
            atlas, fab, fabless, iron, maya, rex, trump, vera, volt, energy,
            commodity. Each agent sees only its own section.
        sector_rotation: One paragraph on sector leadership.
        overnight_notes: Observations on overnight positions (if any).
    """
    from datetime import datetime, timezone

    date_str = _resolve_date(date)
    analysis_dir = Path("data/mike_analysis")
    analysis_dir.mkdir(parents=True, exist_ok=True)
    txt_path = analysis_dir / f"{date_str}.txt"
    json_path = analysis_dir / f"{date_str}.json"

    async with _mike_lock(date_str):
        # 1. Append free-form analysis to the .txt file.
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        separator = f"\n\n{'='*60}\nWRITTEN AT: {now_utc}\n{'='*60}\n\n"
        tmp_path = txt_path.with_suffix(".txt.tmp")
        existing = txt_path.read_text(encoding="utf-8") if txt_path.exists() else ""
        tmp_path.write_text(existing + separator + analysis, encoding="utf-8")
        tmp_path.replace(txt_path)

        # 2. Merge structured fields into the .json file.
        structured: dict = {}
        if json_path.exists():
            try:
                structured = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception:
                structured = {}

        updates = {
            "date": date_str,
            "last_updated_utc": now_utc,
            "regime": regime,
            "risk_tone": risk_tone,
            "sector_rotation": sector_rotation,
            "atlas_guidance": atlas_guidance,
            "fab_guidance": fab_guidance,
            "fabless_guidance": fabless_guidance,
            "iron_guidance": iron_guidance,
            "maya_guidance": maya_guidance,
            "rex_guidance": rex_guidance,
            "trump_guidance": trump_guidance,
            "vera_guidance": vera_guidance,
            "volt_guidance": volt_guidance,
            "energy_guidance": energy_guidance,
            "commodity_guidance": commodity_guidance,
            "overnight_notes": overnight_notes,
        }
        for k, v in updates.items():
            if v is not None:
                structured[k] = v
        # Append a writes log so we can see morning vs midday updates.
        writes = structured.get("writes", [])
        writes.append({
            "at_utc": now_utc,
            "regime": regime,
            "fields_set": [k for k, v in updates.items() if v is not None and k != "date"],
        })
        structured["writes"] = writes[-10:]  # cap history

        tmp_json = json_path.with_suffix(".json.tmp")
        tmp_json.write_text(json.dumps(structured, indent=2), encoding="utf-8")
        tmp_json.replace(json_path)

    return json.dumps({
        "status": "written",
        "txt_path": str(txt_path),
        "json_path": str(json_path),
        "date": date_str,
        "bytes_written": len(analysis),
        "regime": structured.get("regime"),
    })


@mcp.tool()
async def get_mike_analysis(date: str = "today", agent_name: Optional[str] = None) -> str:
    """
    Retrieve Mike's market analysis. If `agent_name` is any of the 11 sector
    agents (atlas, fab, fabless, iron, maya, rex, trump, vera, volt, energy,
    commodity), returns only that agent's guidance + regime + risk_tone
    (compact view). Otherwise returns the full structured JSON + full text.

    Returns an advisory message if Mike hasn't written for this date yet.

    Args:
        date: 'today' (default, America/New_York) or 'YYYY-MM-DD'.
        agent_name: Optional — one of the 11 sector agents for per-agent view.
    """
    date_str = _resolve_date(date)
    base = Path("data/mike_analysis")
    json_path = base / f"{date_str}.json"
    txt_path = base / f"{date_str}.txt"

    if not json_path.exists() and not txt_path.exists():
        return json.dumps({
            "status": "not_found",
            "date": date_str,
            "analysis": (
                f"Mike has not written an analysis for {date_str} yet. "
                "Proceed without director guidance — apply conservative defaults: "
                "reduce position sizes by 20%, avoid high-conviction macro bets, "
                "prefer intraday over overnight holds."
            ),
        })

    structured: dict = {}
    if json_path.exists():
        try:
            structured = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            structured = {}

    if agent_name:
        guidance_key = f"{agent_name.lower()}_guidance"
        return json.dumps({
            "status": "found",
            "date": date_str,
            "agent_name": agent_name.lower(),
            "regime": structured.get("regime"),
            "risk_tone": structured.get("risk_tone"),
            "guidance": structured.get(guidance_key, "No specific guidance written for this trader."),
            "last_updated_utc": structured.get("last_updated_utc"),
        })

    full_text = txt_path.read_text(encoding="utf-8") if txt_path.exists() else ""
    return json.dumps({
        "status": "found",
        "date": date_str,
        "regime": structured.get("regime"),
        "risk_tone": structured.get("risk_tone"),
        "structured": structured,
        "analysis": full_text,
        "bytes": len(full_text),
    })


@mcp.tool()
async def list_mike_analyses(limit: int = 14) -> str:
    """
    Return the most recent Mike analyses — date + regime call + one-line risk tone.
    Useful for reviewing Mike's recent calls without opening each file.

    Args:
        limit: Max entries to return (default 14).
    """
    base = Path("data/mike_analysis")
    if not base.exists():
        return json.dumps({"entries": [], "count": 0})

    entries = []
    for p in sorted(base.glob("*.json"), reverse=True)[:limit]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        entries.append({
            "date": data.get("date", p.stem),
            "regime": data.get("regime"),
            "risk_tone": data.get("risk_tone"),
            "last_updated_utc": data.get("last_updated_utc"),
        })
    return json.dumps({"entries": entries, "count": len(entries)})


@mcp.tool()
async def get_quiet_window() -> str:
    """
    Return the configured quiet-window bounds (from config.yaml) as UTC HH:MM strings.
    Used by every scheduled command to decide whether to exit silently.
    """
    await _ensure_init_light()
    qw = _cfg.get("quiet_window", {}) or {}
    return json.dumps({
        "az_start": qw.get("az_start", "22:00"),
        "az_end": qw.get("az_end", "05:00"),
        "utc_start": qw.get("utc_start", "05:00"),
        "utc_end": qw.get("utc_end", "12:00"),
        "description": qw.get("description", "No autonomous activity 10pm–5am Arizona"),
    })


# ── Agent thesis journal ──────────────────────────────────────────────────────

@mcp.tool()
async def record_thesis(
    agent_name: str,
    kind: str,
    title: str,
    body: str,
    verify_by: Optional[str] = None,
    parent_id: Optional[int] = None,
    market_snapshot: Optional[dict] = None,
    primary_symbol: Optional[str] = None,
    direction: Optional[str] = None,
    entry_price: Optional[float] = None,
) -> str:
    """
    Append an entry to your private thesis journal. Append-only — to revise, post a new
    entry with parent_id pointing at the prior one and update the prior one's status to
    'superseded'. Each agent reads only its own journal in its review prompt; Mike reads
    all journals when writing morning analysis.

    Args:
        agent_name: Your agent name (e.g. 'rex'). Server does not auto-attribute.
        kind: 'hypothesis' (a view to test), 'prediction' (specific outcome with date),
              'observation' (something you noticed), 'question' (open inquiry to revisit).
        title: Short label, <80 chars, e.g. "NVDA leadership intact while above $670".
        body: Full reasoning. Cite levels, fills, technicals.
        verify_by: ISO date 'YYYY-MM-DD' when this should be evaluated. Required for
                   'prediction' kind; optional for others.
        parent_id: ID of an existing entry this refines or supersedes.
        market_snapshot: Optional dict with context at write time, e.g.
                         {"nav": 43950, "regime": "BULLISH", "spy": 715.20, "vix": 17.4}.
        primary_symbol: Ticker the thesis claims a view on (e.g. 'XOM'). When set
                        together with `direction`, the nightly thesis_resolver
                        verifies the call against bars instead of trusting your
                        self-grade.
        direction: 'long' or 'short' — what move would confirm the thesis. Required
                   for price-anchored verification.
        entry_price: Reference close for the verifier. If omitted but `primary_symbol`
                     is set, the server snapshots get_quote() now. Pass explicitly
                     when you want a level that isn't the latest tick (e.g. a
                     prior close).
    """
    await _ensure_init_light()
    from db import store
    sym = (primary_symbol or "").upper() or None
    # Cross-check entry_price against a live quote whenever symbol+direction are
    # provided. Agents repeatedly fabricated anchors (e.g. AAPL=198.45 stamped
    # across 14 hourly theses with real last=298, or DIA=27.5 vs real 497) and
    # the resolver mis-graded them. Now: agent passes nothing → use live; agent
    # passes within 10% of live → accept; agent passes >10% off → overwrite
    # with live + warn. Quote failures leave the agent's value untouched.
    if sym and direction:
        try:
            from data.massive_client import get_quote
            q = await get_quote(sym)
            last = q.get("last") or q.get("price") or q.get("close")
            if last is not None:
                live = float(last)
                if entry_price is None:
                    entry_price = live
                elif live and abs(float(entry_price) - live) / live > 0.10:
                    log.warning(
                        "record_thesis: agent=%s sym=%s entry_price=%.4f deviates >10%% from live %.4f — overwriting",
                        agent_name, sym, float(entry_price), live,
                    )
                    entry_price = live
        except Exception as exc:
            log.warning("record_thesis: get_quote(%s) failed: %s — entry_price unchanged", sym, exc)
    thesis_id = await store.record_thesis(
        agent_name=agent_name,
        kind=kind,
        title=title,
        body=body,
        verify_by=verify_by,
        parent_id=parent_id,
        market_snapshot=market_snapshot,
        primary_symbol=sym,
        direction=direction,
        entry_price=entry_price,
    )
    return json.dumps({
        "thesis_id": thesis_id,
        "primary_symbol": sym,
        "direction": direction,
        "entry_price": entry_price,
    })


@mcp.tool()
async def update_thesis_status(
    thesis_id: int,
    status: str,
    resolution_note: str,
    agent_name: str,
) -> str:
    """
    Mark an open thesis entry as confirmed/wrong/superseded. Only the owning agent can
    update its own entries. Use this when verify_by date passes and you have the verdict.

    Args:
        thesis_id: ID of the entry to update.
        status: 'confirmed' (came true), 'wrong' (didn't), 'superseded' (replaced by a
                newer entry; pass parent_id on the new record_thesis call).
        resolution_note: 1-2 sentences with the evidence (price level, fill, news event).
        agent_name: Your agent name. Server enforces ownership.
    """
    await _ensure_init_light()
    from db import store
    updated = await store.update_thesis_status(
        thesis_id=thesis_id, status=status, resolution_note=resolution_note,
        agent_name=agent_name,
    )
    return json.dumps({"updated": updated})


@mcp.tool()
async def get_my_journal(agent_name: str) -> str:
    """
    Return your journal slice for prompt continuity: open theses (top 10),
    predictions due for verification today or earlier, and last 3 resolutions.
    Each agent should call this at the start of every review.

    Args:
        agent_name: Your agent name.
    """
    await _ensure_init_light()
    from datetime import date as _date
    from db import store
    today = _date.today().isoformat()
    open_theses = await store.get_open_theses(agent_name, limit=10)
    due = await store.get_theses_due(agent_name, on_or_before=today)
    resolved = await store.get_recent_resolutions(agent_name, limit=3)
    return json.dumps(
        {"open": open_theses, "due_today_or_earlier": due, "recent_resolutions": resolved},
        default=str,
    )


@mcp.tool()
async def get_all_journals(caller: str) -> str:
    """
    Return all agents' open theses grouped by agent. Mike-only — used in mike-morning
    to spot cross-desk patterns. Server rejects callers other than 'mike'.

    Args:
        caller: Must be 'mike'. Other values return an empty payload.
    """
    await _ensure_init_light()
    if caller != "mike":
        return json.dumps({"error": "get_all_journals is mike-only", "error_code": "permission", "journals": {}})
    from db import store
    return json.dumps({"journals": await store.get_all_open_theses()}, default=str)


# ── Agent inbox (dashboard chat → per-agent question routing) ────────────────

@mcp.tool()
async def get_my_inbox(agent_name: str) -> str:
    """
    Read pending dashboard questions addressed to your agent. Used by the
    /<agent>-respond skill: each pending row is a question the user typed into
    your dashboard cell. After answering, call mark_inbox_responded.

    Args:
        agent_name: Your agent name (e.g. 'atlas', 'rex').
    """
    await _ensure_init_light()
    from db import store
    pending = await store.get_pending_inbox(agent_name)
    return json.dumps({"pending": pending}, default=str)


@mcp.tool()
async def mark_inbox_responded(
    inbox_id: int,
    response_body: str,
    agent_name: str,
) -> str:
    """
    Mark a pending inbox row as responded with the agent's reply text. The
    dashboard surfaces the reply in the agent's "Recent Q&A" expander on next
    refresh. Server enforces ownership: only the agent named on the row can
    respond, and only while the row is still 'pending'.

    Args:
        inbox_id: ID of the pending row (from get_my_inbox).
        response_body: Your full reply (1-3 paragraphs).
        agent_name: Your agent name. Must match the row's agent_name.
    """
    await _ensure_init_light()
    from db import store
    updated = await store.mark_inbox_responded(
        inbox_id=inbox_id, response_body=response_body, agent_name=agent_name,
    )
    return json.dumps({"updated": updated})


# ── Tool-gap requests ─────────────────────────────────────────────────────────

@mcp.tool()
async def raise_tool_gap(
    agent_name: str,
    tool_name: str,
    description: str,
    use_case: str,
    priority: str = "normal",
) -> str:
    """
    Record a request for a tool that doesn't exist yet. Mike reads these in morning
    analysis and consolidates them into his digest to the user. Do NOT Telegram the
    user directly for tool requests — go through this channel so Mike can dedupe.

    Args:
        agent_name: Your agent name.
        tool_name: Short proposed name, e.g. 'get_options_chain', 'compute_iv_rank'.
        description: What the tool would do, what it returns.
        use_case: Why YOU need it — concrete situation where the lack hurt your trade.
        priority: 'low' | 'normal' | 'high'. Reserve 'high' for blocked-without-it.
    """
    await _ensure_init_light()
    from db import store
    gap_id = await store.record_tool_gap(
        agent_name=agent_name, tool_name=tool_name,
        description=description, use_case=use_case, priority=priority,
    )
    return json.dumps({"gap_id": gap_id})


@mcp.tool()
async def list_open_tool_gaps(caller: str) -> str:
    """
    List all open tool-gap requests. Mike-only. Used in mike-morning consolidation.

    Args:
        caller: Must be 'mike'.
    """
    await _ensure_init_light()
    if caller != "mike":
        return json.dumps({"error": "list_open_tool_gaps is mike-only", "error_code": "permission", "gaps": []})
    from db import store
    return json.dumps({"gaps": await store.list_open_tool_gaps()}, default=str)


@mcp.tool()
async def update_tool_gap_status(
    gap_id: int,
    status: str,
    mike_note: Optional[str] = None,
    caller: str = "",
) -> str:
    """
    Update a tool-gap status. Mike-only.

    Args:
        gap_id: ID of the gap.
        status: 'acknowledged' (Mike has seen it), 'forwarded' (included in user-facing
                digest), 'implemented' (tool now exists), 'declined' (won't build).
        mike_note: Optional consolidation note (e.g. "merged with #14").
        caller: Must be 'mike'.
    """
    await _ensure_init_light()
    if caller != "mike":
        return json.dumps({"error": "update_tool_gap_status is mike-only", "error_code": "permission", "updated": False})
    from db import store
    updated = await store.update_tool_gap_status(gap_id, status, mike_note)
    return json.dumps({"updated": updated})


# ── Evening digests ───────────────────────────────────────────────────────────

@mcp.tool()
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
) -> str:
    """
    Persist your evening summary for the day. One row per (agent, trading_date) — calling
    twice for the same day overwrites.

    Args:
        agent_name: Your agent name.
        trading_date: 'YYYY-MM-DD' — usually today.
        thesis_summary: 'What I learned today' — 2-4 sentences.
        open_questions: 'Carry-forward to tomorrow' — bullets.
        tomorrow_focus: 'What I'll watch / what would put me in' — bullets with triggers.
        pnl_today / pnl_week: numeric P&L snapshots.
        positions: list of {symbol, qty, avg_cost, market_value, unrealized_pnl}.
        chart_path: relative path to a PNG generated for this digest, e.g.
                    'data/charts/rex_2026-04-27.png'.
    """
    await _ensure_init_light()
    from db import store
    digest_id = await store.record_evening_digest(
        agent_name=agent_name, trading_date=trading_date,
        thesis_summary=thesis_summary, open_questions=open_questions,
        tomorrow_focus=tomorrow_focus, pnl_today=pnl_today, pnl_week=pnl_week,
        positions=positions, chart_path=chart_path,
    )
    return json.dumps({"digest_id": digest_id})


# ── Telegram chart ────────────────────────────────────────────────────────────

@mcp.tool()
async def send_telegram_chart(
    image_path: str,
    caption: Optional[str] = None,
    author_agent: Optional[str] = None,
) -> str:
    """
    Send an image (PNG/JPG) to Telegram via sendPhoto. Use for end-of-day chart digests.

    Args:
        image_path: Path to image file (relative to repo root or absolute).
        caption: Optional caption text, <1024 chars. Plain text is safest; Telegram's
                 Markdown parsing is finicky with underscores and special chars.
        author_agent: Optional agent identifier ('mike', 'atlas', 'cassidy', …)
                      for the telegram_message audit log. Defaults to 'system'.
    """
    await _ensure_init_light()
    from approval.telegram import send_photo
    result = await send_photo(
        image_path, caption,
        kind="push",
        meta={"author_agent": author_agent or "system"},
    )
    return json.dumps({"sent": result is not None})


@mcp.tool()
async def generate_agent_chart(agent_name: str, date: Optional[str] = None) -> str:
    """
    Generate a 30-day performance chart PNG for one agent and return its file path.

    Runs reporting/agent_chart.py as a subprocess (matplotlib, asyncpg). The chart
    shows cumulative attributed P&L (top panel) and daily net P&L bars (bottom panel)
    over the last 30 calendar days, plus overall prediction hit rate in the title.

    Args:
        agent_name: Sector agent name (e.g. "rex", "atlas").
        date: Chart date YYYY-MM-DD (default: today).

    Returns:
        {"chart_path": "data/charts/{agent}_{date}.png"}
        {"error": "..."} on failure.
    """
    import asyncio as _asyncio
    from datetime import date as _date

    d = date or _date.today().isoformat()
    proc = await _asyncio.create_subprocess_exec(
        sys.executable, "-m", "reporting.agent_chart",
        "--agent", agent_name, "--date", d,
        stdout=_asyncio.subprocess.PIPE,
        stderr=_asyncio.subprocess.PIPE,
        cwd=str(Path(__file__).parent),
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return _err(stderr.decode().strip())
    return json.dumps({"chart_path": stdout.decode().strip()})


@mcp.tool()
async def generate_evening_slide(
    agent_name: str,
    headline: str,
    macro_thesis: Optional[list] = None,
    trends: Optional[list] = None,
    theses: Optional[list] = None,
    philosophy: Optional[list] = None,
    open_questions: Optional[list] = None,
) -> str:
    """
    Render the agent's 1-page evening summary slide and return its path.

    Layout:
      - Header banner (agent name + date + headline P&L).
      - Top "Today's thesis" panel — 2-3 prose bullets in human language,
        the agent's fundamental read on what's happening in the world,
        sector, or specific names.
      - Hourly combined-P&L chart (left, trading-hours-compressed).
      - Top-N forecast panel with per-ticker indicator overlays (right).
      - Four bullet panels at the bottom — trends/catalysts, theses,
        trading philosophy, open questions/waiting-on.

    Use this in the evening review INSTEAD OF sending the P&L chart and
    forecast panel as separate Telegram messages. One message per agent.

    Args:
        agent_name: Sector agent name (e.g., "fab", "vera").
        headline: One-line P&L summary, e.g.
                  "P&L: -$155 today (real -$18 / unreal -$137, 4 positions)".
        macro_thesis: 2-3 prose bullets for the top panel. When omitted,
                      auto-aggregates from agent_thesis records of the
                      past 24h (kind IN ('thesis','observation')).
                      Override only when you want fresh prose at EOD that
                      isn't already in the journal.
        trends: bullets — news, catalysts, sector tape (3-6 short items).
        theses: bullets — your active framework calls (3-6 short items).
        philosophy: bullets — sizing rules / style notes in play this hour.
        open_questions: bullets — unresolved questions, calendar events
                        you're waiting on, data gaps.

    Returns: {"chart_path": "data/charts/slide_{agent}_{stamp}.png"} or
             {"error": "..."}.
    """
    await _ensure_init_light()
    from reporting.evening_slide import render_evening_slide
    try:
        path = await render_evening_slide(
            agent_name,
            headline=headline,
            macro_thesis=list(macro_thesis) if macro_thesis is not None else None,
            trends=list(trends or []),
            theses=list(theses or []),
            philosophy=list(philosophy or []),
            open_questions=list(open_questions or []),
        )
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", code="internal")
    if path is None:
        return _err("slide not produced (component charts missing)")
    return json.dumps({"chart_path": str(path)})


@mcp.tool()
async def generate_forecast_panel(agent_name: str) -> str:
    """
    Build the agent's top-10 forecast panel PNG and return its path.

    One row per ticker, stacked vertically. Each row shows the last 5 trading
    days of close prices plus a dashed forecast line extending to the
    forecast's `time_to_target_days`, ending at
        today_price × (1 + expected_return_pct/100 × likelihood).
    A vertical horizon marker labels `time_to_target_days = N` so the agent's
    timescale is legible at a glance. Tickers are picked from
    (active forecasts ∪ current positions), deduped, ranked by abs(market
    value) primary and the desk's internal allocator weight secondary.

    Use in the evening review alongside `generate_pnl_curve` /
    `generate_agent_chart`. Returns {"chart_path": "..."} or
    {"error": "..."} on failure or {"empty": true} when the agent has no
    active forecasts and no positions.

    Args:
        agent_name: Sector agent name (e.g. "fab", "fabless", "vera").
    """
    await _ensure_init_light()
    from reporting.forecast_panel import render_forecast_panel
    try:
        path = await render_forecast_panel(agent_name)
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", code="internal")
    if path is None:
        return json.dumps({"empty": True, "agent": agent_name})
    return json.dumps({"chart_path": str(path)})


@mcp.tool()
async def generate_pnl_curve(
    agent_name: Optional[str] = None,
    since: str = "7d",
) -> str:
    """
    Render an hour-by-hour P&L curve PNG from `agent_state` snapshots and
    return its path. Shows realized (line) + total (line) with a shaded
    band representing unrealized — green when total > realized (gain on
    open positions), red when total < realized (drawdown on open positions).

    Args:
        agent_name: Agent name (e.g. "rex", "maya"). Omit / null for the
                    desk-aggregated curve summed across all agents per hour.
        since: Window — ISO timestamp ("2026-05-01T00:00:00Z") or duration
               ("1d", "24h", "7d", "30d", "all"). Default "7d".

    Returns:
        {"chart_path": "data/charts/pnl_curve_..._YYYYMMDD_HHMMSS.png"}
        {"error": "..."} on failure.
    """
    await _ensure_init_light()
    from reporting.pnl_curve import render_agent_curve, render_desk_curve
    try:
        if agent_name:
            path = await render_agent_curve(agent_name, since=since)
        else:
            path = await render_desk_curve(since=since)
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", code="internal")
    return json.dumps({"chart_path": str(path)})


# ── Custom indicator dispatch ─────────────────────────────────────────────────

@mcp.tool()
async def compute_custom_indicator(
    agent_name: str,
    model_name: str,
    symbol: str,
    bar_size: str = "1 day",
    duration: str = "3 M",
) -> str:
    """
    Run an agent-authored indicator from agents/<agent_name>/models/<model_name>.py.
    The module must expose a function: compute(symbol, bars, context) -> dict.

    Bars are fetched by the server (same pipeline as get_bars). Context contains
    {nav, regime} where available. Output is whatever dict the model returns.

    Args:
        agent_name: Your agent name. Server enforces that the module lives under
                    agents/<agent_name>/models/.
        model_name: Filename without .py, e.g. 'breakout_strength'.
        symbol: Ticker.
        bar_size: '1 min', '5 mins', '15 mins', '1 hour', '1 day'.
        duration: '1 D', '5 D', '1 M', '3 M', '1 Y'.
    """
    await _ensure_init()
    import importlib
    import re
    from data.massive_client import get_bars as _get_bars
    from ibkr.account import get_account_summary

    # Reject path traversal / arbitrary import — agent_name and model_name flow
    # straight into importlib.import_module, so anything other than plain
    # identifiers could load arbitrary Python from elsewhere on disk.
    _ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
    if not _ID_RE.match(agent_name or "") or not _ID_RE.match(model_name or ""):
        return _err("invalid agent_name or model_name (must match [a-z][a-z0-9_]{0,31})", code="validation")

    module_path = Path("agents") / agent_name / "models" / f"{model_name}.py"
    try:
        agents_root = Path("agents").resolve()
        resolved = module_path.resolve()
        if agents_root not in resolved.parents:
            return _err("model path escapes agents/ tree")
    except (OSError, ValueError):
        return _err("invalid model path")
    if not module_path.exists():
        return _err(f"model not found: {module_path}", code="not_found")

    bars_response = await _get_bars(symbol, bar_size, duration, "TRADES")
    # massive_client returns {symbol, bar_size, duration, bars: [...]}.
    # Pass just the inner list to the model — simpler convention.
    bars = bars_response.get("bars", []) if isinstance(bars_response, dict) else bars_response
    summary = await get_account_summary()

    # Build context — pull regime from today's mike_analysis if present.
    regime = None
    try:
        mike_path = Path("data/mike_analysis") / f"{_market_date()}.json"
        if mike_path.exists():
            with open(mike_path, "r", encoding="utf-8") as f:
                regime = (json.load(f) or {}).get("regime")
    except Exception:
        pass
    context = {"nav": summary.get("nav"), "regime": regime, "agent_name": agent_name}

    try:
        module = importlib.import_module(f"agents.{agent_name}.models.{model_name}")
        importlib.reload(module)  # pick up edits without restarting the server
        result = module.compute(symbol, bars, context)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}", code="internal", model=model_name)

    return json.dumps({"model": model_name, "symbol": symbol, "result": result}, default=str)


@mcp.tool()
async def compute_all_models(
    agent_name: str,
    symbol: str,
    bar_size: str = "1 day",
    duration: str = "1 Y",
) -> str:
    """
    Auto-discover and run every quant model in agents/<agent_name>/models/.

    For each .py file in the directory that exposes compute(symbol, bars, context),
    the model is invoked with (symbol, bars, context) and its result is collected.
    Per-model failures are isolated — one bad model does NOT block the others.
    Modules are reloaded each call so model edits land without an MCP server restart.

    Returns JSON of shape:
        {
          "agent": "<agent_name>",
          "symbol": "<SYM>",
          "error_count": <int — # of models whose error is not null>,
          "errored_models": ["<name>", ...],
          "flat_count": <int — # of models whose result.direction == 'flat'>,
          "models": {
            "<model_name>": {
              "version": "X.Y" | "unset",
              "result": <whatever model.compute returns> | null,
              "error": "TypeError: ..." | null
            },
            ...
          }
        }

    The top-level error_count / errored_models / flat_count fields are summary
    signals — agents must check error_count >= 1 and apply the BROKEN MODEL
    DECISION RULE (see desk policy) before using ANY model output. flat_count
    on a single symbol is a per-call signal; if every model returns flat across
    a sweep of N symbols, that's a portfolio problem — see QUANT ENGAGEMENT
    DOCTRINE for the universe-flatness check.

    Sector-review skills should call this once per symbol and reason across the
    portfolio — where models agree (high-conviction setup), where they disagree
    (information; pick a side and justify), and where one errors (fix inline if
    <30 lines + one-sentence diagnosis, otherwise file a model:* observation
    thesis + tool gap before continuing). For targeting a specific model by name
    (debugging, model-tune verification), use compute_custom_indicator instead.

    Bar window defaults to 1 Y (~252 trading days of daily bars). This window is
    sized to cover the deepest model in any sector portfolio (fab/iron use SMA_200,
    needing 200 bars). The earlier 3 M default starved 200-bar models silently —
    raised by fab as model:equipment_cycle:bars_underflow (thesis #214 2026-05-05).
    If you add a model that needs more than 252 bars, pass duration="2 Y".

    Args:
        agent_name: Sector agent (e.g. 'atlas', 'fab', 'energy').
        symbol: Ticker.
        bar_size: '1 min', '5 mins', '15 mins', '1 hour', '1 day'.
        duration: '1 D', '5 D', '1 M', '3 M', '1 Y' (3 M ≈ 60 trading days).
    """
    await _ensure_init()
    import re
    from data.massive_client import get_bars as _get_bars
    from ibkr.account import get_account_summary
    from meta_agent.model_loader import run_all_models

    _ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
    if not _ID_RE.match(agent_name or ""):
        return _err("invalid agent_name (must match [a-z][a-z0-9_]{0,31})", code="validation")

    bars_response = await _get_bars(symbol, bar_size, duration, "TRADES")
    bars = bars_response.get("bars", []) if isinstance(bars_response, dict) else bars_response
    summary = await get_account_summary()

    # Build context — same shape as compute_custom_indicator (regime from today's
    # mike_analysis if present).
    regime = None
    try:
        mike_path = Path("data/mike_analysis") / f"{_market_date()}.json"
        if mike_path.exists():
            with open(mike_path, "r", encoding="utf-8") as f:
                regime = (json.load(f) or {}).get("regime")
    except Exception:
        pass
    context = {"nav": summary.get("nav"), "regime": regime, "agent_name": agent_name}

    results = run_all_models(agent_name, symbol, bars, context)
    error_count = sum(1 for m in results.values() if m.get("error"))
    errored_models = [name for name, m in results.items() if m.get("error")]
    flat_count = sum(
        1 for m in results.values()
        if isinstance(m.get("result"), dict) and m["result"].get("direction") == "flat"
    )
    return json.dumps(
        {
            "agent": agent_name,
            "symbol": symbol,
            "error_count": error_count,
            "errored_models": errored_models,
            "flat_count": flat_count,
            "models": results,
        },
        default=str,
    )


# ── Conviction views (sector-shard architecture) ─────────────────────────────
#
# Sector agents publish signed conviction views per symbol; Mike (the allocator)
# reads the consolidated view and rebalances the desk. Agents no longer place
# orders directly — see place_order's gate (Stage 3).

_SECTOR_MAP_CACHE: Optional[dict] = None
_SECTOR_MAP_CACHED_AT: float = 0.0
_SECTOR_MAP_TTL_S: float = 30.0

_INVERSE_MAP_CACHE: Optional[dict] = None
_INVERSE_MAP_MTIME: float = 0.0


async def _load_sector_map() -> dict:
    """Load the canonical sector map from agent_watchlist (SQL). Returns the
    same shape sector_map.yaml exposed: {"agents": {<agent>: {"universe":
    {<SYMBOL>: {"bearish_via": ...}}}}}. Cached 30s so hot callers (allocator
    rebalance, conviction validation) don't hammer the DB."""
    global _SECTOR_MAP_CACHE, _SECTOR_MAP_CACHED_AT
    import time as _time
    now = _time.time()
    if _SECTOR_MAP_CACHE is not None and (now - _SECTOR_MAP_CACHED_AT) < _SECTOR_MAP_TTL_S:
        return _SECTOR_MAP_CACHE
    from db import store
    _SECTOR_MAP_CACHE = await store.load_watchlist_as_sector_map()
    _SECTOR_MAP_CACHED_AT = now
    return _SECTOR_MAP_CACHE


def _load_inverse_map() -> dict:
    """Load agents/inverse_etf_map.yaml; cache by mtime."""
    global _INVERSE_MAP_CACHE, _INVERSE_MAP_MTIME
    path = Path("agents") / "inverse_etf_map.yaml"
    if not path.exists():
        return {}
    mtime = path.stat().st_mtime
    if _INVERSE_MAP_CACHE is None or mtime != _INVERSE_MAP_MTIME:
        with open(path, "r", encoding="utf-8") as f:
            _INVERSE_MAP_CACHE = yaml.safe_load(f) or {}
        _INVERSE_MAP_MTIME = mtime
    return _INVERSE_MAP_CACHE


_SYMBOL_RE = __import__("re").compile(r"^[A-Z][A-Z0-9.\-]{0,11}$")


def _validate_symbol(symbol: str) -> tuple[bool, str]:
    """Cheap input filter: reject obvious junk (whitespace, shell metas, path
    fragments) before any downstream tool touches the value. Permissive enough
    to allow class-share dots (BRK.B) and 12-char tickers, strict enough that
    no validated symbol can carry control chars or null bytes."""
    if not isinstance(symbol, str) or not symbol:
        return False, "symbol must be a non-empty string"
    s = symbol.strip().upper()
    if not _SYMBOL_RE.match(s):
        return False, f"invalid symbol format: {symbol!r}"
    return True, ""


def _validate_rationale(rationale: str, max_len: int = 2048) -> tuple[bool, str]:
    """Cap rationale length and ban control chars. Without this, a runaway agent
    can poison the audit log, blow Telegram's 4096-char message ceiling, or
    sneak hidden newlines past downstream consumers."""
    if not isinstance(rationale, str):
        return False, "rationale must be a string"
    if len(rationale) > max_len:
        return False, f"rationale too long ({len(rationale)} > {max_len} chars)"
    if "\x00" in rationale:
        return False, "rationale contains null bytes"
    return True, ""


async def _agent_owns_symbol(agent_name: str, symbol: str) -> tuple[bool, str]:
    """Returns (allowed, reason). Mike may submit views on any symbol (tactical hedges).
    Sector agents may submit on (a) any symbol in their watchlist or (b) any verified
    inverse ETF from agents/inverse_etf_map.yaml — the desk's NO-DIRECT-SHORTS policy
    routes bearish convictions through long-on-inverse, so the inverse catalog is
    universe-agnostic. Canonical universe lives in the agent_watchlist table
    (seeded once from sector_map.yaml; live edits via add_to_watchlist /
    propose_watchlist_removal)."""
    if agent_name == "mike":
        return True, ""
    sym = symbol.upper()
    # CASH is a reserved pseudo-symbol — every agent may submit cash conviction.
    if sym == "CASH":
        return True, ""
    from db import store
    if await store.agent_owns_symbol_db(agent_name, sym):
        return True, ""
    inverse_map = _load_inverse_map() or {}
    inverses = inverse_map.get("inverses") or {}
    entry = inverses.get(sym) or inverses.get(symbol)
    if entry and entry.get("verified") is True:
        return True, ""
    return False, f"{sym} is not in {agent_name}'s watchlist and not a verified inverse ETF (see agent_watchlist table + agents/inverse_etf_map.yaml)"


@mcp.tool()
async def submit_conviction_view(
    agent_name: str,
    symbol: str,
    direction: str,
    rationale: str,
    expires_in_hours: float,
    expected_return_pct: Optional[float] = None,
    likelihood: Optional[float] = None,
    time_to_target_days: Optional[int] = None,
    model_inputs: Optional[dict] = None,
    momentum_confirmed: Optional[bool] = None,
    stop_pct: Optional[float] = None,
    session_id: Optional[str] = None,
) -> str:
    """
    Publish a signed forecast on one symbol. Upserts on (agent_name, symbol)
    so calling again replaces the prior row. Mike reads these to size the desk.

    AGENTS DO NOT PICK THE ALLOCATOR WEIGHT. You supply the forecast triple —
    (expected_return_pct, likelihood, time_to_target_days) — and the server
    computes the desk's internal weight centrally as:

        weight = abs(expected_return_pct) × likelihood / time_to_target_days

    See `meta_agent.allocator.compute_conviction`. This guarantees every
    agent uses the same scale and removes a hand-tuning degree of freedom
    that the 2026-05 audit flagged.

    Args:
        agent_name: Your agent name (e.g. 'atlas', 'fab', 'vera').
        symbol: Ticker, will be uppercased.
        direction: 'long' | 'short' | 'flat'. Bearish views go through
                   inverse-ETF longs, not direction='short'. 'flat' is the
                   canonical "no view" submission and bypasses the weight
                   formula.
        rationale: 1–2 sentence why (audit trail). For inverse-ETF symbols this is
                   what the user reads on the Telegram approval prompt — be concrete.
        expires_in_hours: REQUIRED. Auto-expire after N hours. Range 0.0833 (5min)
                          to 720 (30 days). Must match your thesis horizon: a
                          scalp and a swing should NOT get the same expiry.
                          Off-hours expirations are no-ops (market_hours risk
                          check blocks SELLs outside RTH).
        expected_return_pct: REQUIRED for direction != 'flat'. Your forecast as a
                             signed % move on this name (e.g. +8.5 = expect +8.5%
                             move; -6.0 = expect -6% move). Sign MUST match
                             direction (long → positive, short → negative).
        likelihood: REQUIRED for direction != 'flat'. Your probability in [0,1]
                    that the forecast plays out. 0.5 = coin-flip; 0.8 = strong
                    confidence. The ONLY confidence number you author — the
                    desk's allocator weight is derived from it.
        time_to_target_days: REQUIRED for direction != 'flat' and must be > 0.
                             Your horizon in trading days. Drives both the
                             desk's internal weight and the evening forecast
                             panel.
        model_inputs: Raw quant model output for replay (optional dict).
        momentum_confirmed: For direction='long' on a verified inverse ETF, asserts
                            whether the underlying is already showing the bearish move
                            (True) vs. an early entry ahead of confirmation (False).
                            None on non-inverse symbols.
        stop_pct: Optional defensive auto-flat trigger. If unrealized return on
                  this symbol falls below -stop_pct, the allocator treats this
                  position as flat regardless of whether you re-publish.
                  Recommended for inverse-ETF longs: 8 on 1×, 4 on ≥2×.
    """
    await _ensure_init_light()
    ok, reason = _validate_symbol(symbol)
    if not ok:
        return _err(f"validation: {reason}", code="validation")
    ok, reason = _validate_rationale(rationale)
    if not ok:
        return _err(f"validation: {reason}", code="validation")
    ok, reason = _rate_check("submit_conviction_view")
    if not ok:
        return _err(reason)
    if symbol.upper() == "CASH" and direction != "long":
        return _err("CASH conviction must be direction='long' (cash reserve, not margin)", code="validation")
    # Catch LLM-fabricated technical-indicator blobs slipping in as model_inputs
    # (the 2026-05-12 audit found 23/34 recent rows had keys no model emits).
    from meta_agent.model_inputs_validator import (
        validate as _validate_model_inputs, is_reject_mode as _mi_reject_mode,
    )
    mi_ok, mi_reason = _validate_model_inputs(
        agent_name, model_inputs, symbol=symbol, direction=direction,
    )
    if not mi_ok and _mi_reject_mode():
        return _err(f"model_inputs: {mi_reason}", code="validation")
    # Non-flat convictions MUST carry the forecast triple — these are the inputs
    # to the central conviction formula. CASH is exempt: it's a cash-reserve vote.
    if direction != "flat" and symbol.upper() != "CASH":
        if expected_return_pct is None:
            return _err("validation: expected_return_pct is required for direction != 'flat' (signed %% move you expect, sign matches direction)")
        if likelihood is None:
            return _err("validation: likelihood is required for direction != 'flat' (probability in [0,1] that the forecast plays out)")
        if not (0.0 <= float(likelihood) <= 1.0):
            return _err(f"validation: likelihood must be in [0, 1], got {likelihood}")
        if time_to_target_days is None or int(time_to_target_days) <= 0:
            return _err("validation: time_to_target_days is required and must be > 0 for direction != 'flat' (horizon in trading days)")
        # Sign discipline: long → positive, short → negative.
        if direction == "long" and float(expected_return_pct) < 0:
            return _err(f"validation: direction='long' requires expected_return_pct >= 0, got {expected_return_pct}")
        if direction == "short" and float(expected_return_pct) > 0:
            return _err(f"validation: direction='short' requires expected_return_pct <= 0, got {expected_return_pct}")
    allowed, reason = await _agent_owns_symbol(agent_name, symbol)
    if not allowed:
        return _err(f"watchlist: {reason}", code="validation")
    # Central conviction calculation — the only place a conviction value is
    # derived from agent inputs. Flat / CASH get conviction = 0 / per agent choice.
    from meta_agent.allocator import compute_conviction
    if direction == "flat":
        conviction = 0.0
    elif symbol.upper() == "CASH":
        # CASH carries a "preference weight" only — agents can omit the forecast
        # triple. If they pass a likelihood, use it; else default to 1.0 with
        # a placeholder return so the allocator gets a stable positive value.
        if likelihood is not None and expected_return_pct is not None and time_to_target_days:
            conviction = compute_conviction(expected_return_pct, likelihood, time_to_target_days)
        else:
            conviction = 1.0
    else:
        conviction = compute_conviction(expected_return_pct, likelihood, time_to_target_days)
        if conviction <= 0.0:
            return _err(
                "validation: computed conviction == 0 — check expected_return_pct / "
                "likelihood / time_to_target_days inputs"
            )
    from db import store
    try:
        view_id = await store.upsert_conviction(
            agent_name=agent_name,
            symbol=symbol,
            direction=direction,
            conviction=conviction,
            expected_return_pct=expected_return_pct,
            time_to_target_days=time_to_target_days,
            likelihood=likelihood,
            rationale=rationale,
            model_inputs=model_inputs,
            expires_in_hours=expires_in_hours,
            momentum_confirmed=momentum_confirmed,
            stop_pct=stop_pct,
            session_id=session_id,
        )
        return json.dumps({
            "view_id": view_id,
            "symbol": symbol.upper(),
            "direction": direction,
            "conviction": round(conviction, 4),
        })
    except (ValueError, AssertionError) as e:
        return _err(f"validation: {e}", code="validation")


@mcp.tool()
async def submit_conviction_from_model(
    agent_name: str,
    model_name: str,
    symbol: str,
    rationale: str,
    expires_in_hours: float,
    momentum_confirmed: Optional[bool] = None,
    session_id: Optional[str] = None,
) -> str:
    """
    Publish a forecast whose numeric fields come from running a server-side
    quant model. The agent picks (model, symbol, rationale); compute() returns
    direction / expected_return_pct / likelihood / time_to_target_days /
    stop_pct. The desk's internal allocator weight is then recomputed
    centrally — neither the agent nor the model picks it. See
    agents/MODEL_CONTRACT.md for the model contract.

    Flow: load agents/{agent_name}/models/{model_name}.py → fetch bars at the
    model's declared BAR_FREQUENCY for LOOKBACK_DAYS (defaults 1d / 252) →
    build context {nav, regime, agent_name} → call compute() → if
    signal/direction declines, return {skipped: true}; otherwise insert with
    the model's triple. Agent supplies only `rationale` (prose) and
    `momentum_confirmed` (inverse-ETF entry timing — a human call, not a
    number).

    Args:
        agent_name: Sector agent whose watchlist contains the symbol (agent_watchlist).
        model_name: File under agents/<agent_name>/models/ without .py.
        symbol: Ticker, will be uppercased.
        rationale: 1–2 sentence human gloss. The ONLY agent-authored content.
        expires_in_hours: REQUIRED. Auto-expire after N hours; no default.
                          Range: 0.0833 (5 min) to 720 (30 days). Pick to match
                          your thesis horizon — a scalp and a swing should NOT
                          get the same expiry.
        momentum_confirmed: Inverse-ETF longs only; see submit_conviction_view.

    Returns one of:
        - {view_id, symbol, direction, conviction, expected_return_pct,
           likelihood, time_to_target_days, stop_pct, from_model, model_version}
          where `conviction` is the desk-internal allocator weight computed
          from the triple — NOT a value the model emitted.
        - {skipped: true, reason, model, model_version} — model declined.
        - {error: "..."} — bad input or compute crash.
    """
    await _ensure_init_light()
    import re

    _ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
    if not _ID_RE.match(agent_name or ""):
        return _err("invalid agent_name (must match [a-z][a-z0-9_]{0,31})", code="validation")
    if not _ID_RE.match(model_name or ""):
        return _err("invalid model_name (must match [a-z][a-z0-9_]{0,31})", code="validation")

    ok, reason = _validate_symbol(symbol)
    if not ok:
        return _err(f"validation: {reason}", code="validation")
    ok, reason = _validate_rationale(rationale)
    if not ok:
        return _err(f"validation: {reason}", code="validation")
    ok, reason = _rate_check("submit_conviction_view")
    if not ok:
        return _err(reason)
    allowed, reason = await _agent_owns_symbol(agent_name, symbol)
    if not allowed:
        return _err(f"watchlist: {reason}", code="validation")

    from meta_agent.conviction_from_model import compute_conviction_payload
    res = await compute_conviction_payload(agent_name, model_name, symbol)
    if res["status"] == "error":
        return _err(res["error"])
    if res["status"] == "skipped":
        return json.dumps({
            "skipped": True,
            "reason": res["reason"],
            "model": model_name,
            "model_version": res.get("model_version", "unset"),
        })

    payload = res["payload"]
    from db import store
    try:
        view_id = await store.upsert_conviction(
            agent_name=agent_name,
            symbol=symbol,
            direction=payload["direction"],
            conviction=payload["conviction"],
            expected_return_pct=payload["expected_return_pct"],
            time_to_target_days=payload["time_to_target_days"],
            likelihood=payload.get("likelihood"),
            rationale=rationale,
            model_inputs=payload["model_inputs"],
            expires_in_hours=expires_in_hours,
            momentum_confirmed=momentum_confirmed,
            stop_pct=payload["stop_pct"],
            session_id=session_id,
            forecast_run_id=payload.get("forecast_run_id"),
            functional_name=payload.get("functional_name"),
        )
    except (ValueError, AssertionError) as exc:
        return _err(f"validation: {exc}", code="validation")

    return json.dumps({
        "view_id": view_id,
        "symbol": symbol.upper(),
        "direction": payload["direction"],
        "conviction": payload["conviction"],
        "expected_return_pct": payload["expected_return_pct"],
        "time_to_target_days": payload["time_to_target_days"],
        "stop_pct": payload["stop_pct"],
        "from_model": model_name,
        "model_version": res.get("model_version", "unset"),
        "forecast_run_id": payload.get("forecast_run_id"),
        "functional_name": payload.get("functional_name"),
    })


@mcp.tool()
async def clear_my_views(agent_name: str) -> str:
    """
    Drop all of this agent's active forecast rows. Call at start of each review
    so the new slate fully replaces the old one (rather than mixing stale + fresh).

    Args:
        agent_name: Your agent name.
    """
    await _ensure_init_light()
    from db import store
    deleted = await store.clear_agent_convictions(agent_name)
    return json.dumps({"deleted": deleted})


@mcp.tool()
async def read_my_workspace(agent_name: str) -> str:
    """
    Read the agent's workspace — `agents/<agent>/` notes + data, plus the
    SQL watchlist. Returns a single payload with three sections every
    hourly/evening review should consume as context:

      - notes:     list of {filename, size, mtime, content} for every file
                   under agents/<agent>/notes/ (markdown freeform).
      - watchlist: list of {symbol, bearish_via, source, added_at,
                   added_reason, removal_pending} pulled from the
                   agent_watchlist SQL table. The user or the agent can
                   add via `add_to_watchlist`; removal goes through
                   `propose_watchlist_removal` (Telegram approval).
      - data:      list of {filename, size, mtime} for every file under
                   agents/<agent>/data/ (saved snapshots / CSV exports).
                   Bodies omitted; agent reads specific data files via
                   the standard filesystem if needed.

    Call this at the start of every review so prior context (notes,
    your current watchlist, saved analyses) flows into your analysis.
    """
    await _ensure_init_light()
    base = Path("agents") / agent_name

    notes_dir = base / "notes"
    data_dir = base / "data"

    out: dict = {"agent_name": agent_name, "notes": [], "watchlist": [],
                 "data": []}

    from db import store
    try:
        out["watchlist"] = await store.load_agent_watchlist(agent_name)
    except Exception as e:
        out["watchlist_error"] = f"{type(e).__name__}: {e}"

    if not base.is_dir():
        # The agent has no on-disk workspace yet, but the SQL watchlist is
        # still meaningful — return what we have rather than 404'ing.
        return json.dumps(out, default=str)

    if notes_dir.is_dir():
        for f in sorted(notes_dir.iterdir()):
            if not f.is_file() or f.suffix not in {".md", ".txt"}:
                continue
            try:
                body = f.read_text(encoding="utf-8")
            except Exception as e:
                body = f"(failed to read: {type(e).__name__}: {e})"
            out["notes"].append({
                "filename": f.name,
                "size": f.stat().st_size,
                "mtime": f.stat().st_mtime,
                "content": body[:8000],   # cap each file to 8KB so the
                                          # combined payload stays reasonable
            })
    if data_dir.is_dir():
        for f in sorted(data_dir.iterdir()):
            if not f.is_file():
                continue
            out["data"].append({
                "filename": f.name,
                "size": f.stat().st_size,
                "mtime": f.stat().st_mtime,
            })
    return json.dumps(out, default=str)


@mcp.tool()
async def write_my_note(
    agent_name: str,
    filename: str,
    content: str,
    mode: str = "write",
) -> str:
    """
    Write or append content to a note file under agents/<agent>/notes/.

    Args:
        agent_name: Your agent name. Must match the folder under agents/.
        filename: e.g. "avgo_thesis_draft.md" or "ai_capex_calendar.txt".
                  Must end in .md or .txt. No path separators allowed.
        content: Markdown / plain text body.
        mode: "write" (replace) or "append" (add at end with a separator).

    Returns: {"path": "agents/<agent>/notes/<filename>", "bytes": N}.
    """
    await _ensure_init_light()
    if "/" in filename or ".." in filename:
        return _err("filename must be a bare name; no path separators")
    if not filename.endswith((".md", ".txt")):
        return _err("filename must end in .md or .txt")
    base = Path("agents") / agent_name / "notes"
    if not base.is_dir():
        return _err(f"agents/{agent_name}/notes/ does not exist", code="not_found")
    path = base / filename
    if mode == "append" and path.exists():
        prior = path.read_text(encoding="utf-8")
        new_body = prior.rstrip() + "\n\n---\n\n" + content
        path.write_text(new_body, encoding="utf-8")
    else:
        path.write_text(content, encoding="utf-8")
    return json.dumps({"path": str(path), "bytes": path.stat().st_size})


async def _backfill_ticker_history(
    symbol: str,
    days_intraday: int = 14,
    days_daily: int = 365,
) -> dict:
    """Pull historical OHLCV from Massive for a single symbol and UPSERT into
    both local caches (local_bars 5-min, local_bars_daily). Idempotent —
    repeat calls overwrite the same rows with the latest Massive aggregates.

    Shared between the MCP tool `expand_ticker_history` and the auto-backfill
    branch inside `add_to_watchlist`."""
    from datetime import datetime, timezone
    from data import massive_client
    from db import store

    sym = symbol.strip().upper()
    started = datetime.now(timezone.utc)
    result: dict = {"symbol": sym, "intraday_rows": 0, "daily_rows": 0,
                    "errors": []}

    # 5-min bars
    if days_intraday > 0:
        try:
            data = await massive_client.get_bars(
                sym, bar_size="5 mins", duration=f"{int(days_intraday)} D",
            )
            rows = []
            for b in (data.get("bars") or []):
                ts = b.get("t")
                if not ts or b.get("o") is None or b.get("c") is None:
                    continue
                bt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if bt.tzinfo is None:
                    bt = bt.replace(tzinfo=timezone.utc)
                rows.append({
                    "symbol": sym, "bar_time": bt, "interval": "5min",
                    "open": b["o"], "high": b.get("h") or b["o"],
                    "low": b.get("l") or b["o"], "close": b["c"],
                    "volume": b.get("v") or 0.0,
                })
            result["intraday_rows"] = await store.upsert_local_bars(rows)
        except Exception as e:
            result["errors"].append(f"intraday: {type(e).__name__}: {e}")

    # Daily bars
    if days_daily > 0:
        try:
            data = await massive_client.get_bars(
                sym, bar_size="1 day", duration=f"{int(days_daily)} D",
            )
            rows = []
            for b in (data.get("bars") or []):
                ts = b.get("t")
                if not ts or b.get("o") is None or b.get("c") is None:
                    continue
                bt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if bt.tzinfo is None:
                    bt = bt.replace(tzinfo=timezone.utc)
                rows.append({
                    "symbol": sym, "bar_date": bt.date(),
                    "open": b["o"], "high": b.get("h") or b["o"],
                    "low": b.get("l") or b["o"], "close": b["c"],
                    "volume": b.get("v") or 0.0,
                })
            result["daily_rows"] = await store.upsert_local_bars_daily(rows)
        except Exception as e:
            result["errors"].append(f"daily: {type(e).__name__}: {e}")

    result["duration_ms"] = int(
        (datetime.now(timezone.utc) - started).total_seconds() * 1000
    )
    return result


@mcp.tool()
async def expand_ticker_history(
    symbol: str,
    days_intraday: int = 14,
    days_daily: int = 365,
) -> str:
    """
    Backfill local Postgres caches for one symbol so the dashboard and per-
    ticker tools have history immediately — no waiting for the next streamer
    cycle (5-min) or end-of-day daily ingest.

    Fetches from Massive.com and UPSERTs into:
      - `local_bars`        (5-min bars, default 14 days; matches retention)
      - `local_bars_daily`  (daily bars, default 365 days; matches 1Y view)

    Idempotent — repeat calls overwrite rows with the latest Massive numbers.
    Call this if you've just added a ticker to your watchlist and want the
    Live Trace chart to populate now, or to recover from a streamer gap.
    `add_to_watchlist` calls this automatically when adding a brand-new
    symbol that has no existing local bars, so manual invocation is rarely
    needed.

    Args:
        symbol: Ticker, will be uppercased.
        days_intraday: 5-min lookback in days (default 14; pass 0 to skip).
        days_daily: Daily lookback in days (default 365; pass 0 to skip).

    Returns: {"symbol", "intraday_rows", "daily_rows", "duration_ms",
              "errors": [...]}.
    """
    await _ensure_init_light()
    sym = symbol.strip().upper()
    if not sym:
        return _err("symbol is required")
    if not os.environ.get("MASSIVE_API_KEY", "").strip():
        return _err("MASSIVE_API_KEY missing — cannot fetch bars")
    res = await _backfill_ticker_history(sym, days_intraday, days_daily)
    return json.dumps(res, default=str)


@mcp.tool()
async def add_to_watchlist(agent_name: str, symbol: str, reason: str) -> str:
    """
    Insert a ticker into the agent_watchlist SQL table (or reactivate it
    if it was previously soft-deleted). Auto-backfills local OHLCV caches
    (`local_bars` 5-min, `local_bars_daily` 1Y) for brand-new symbols so
    the Live Trace dashboard and per-ticker tools see history immediately.

    Removing tickers requires user approval via Telegram — call
    `propose_watchlist_removal` instead of trying to delete directly.

    Args:
        agent_name: Your agent name.
        symbol: Ticker to add (will be uppercased). Ownership is not
                validated against sector_map — agents may track anything.
        reason: One short sentence on why this name needs attention.
    """
    await _ensure_init_light()
    sym = symbol.strip().upper()
    if not sym:
        return _err("symbol is required")
    if not (reason or "").strip():
        return _err("reason is required (≥1 sentence)")
    from db import store
    try:
        result = await store.add_watchlist_symbol(
            agent_name=agent_name,
            symbol=sym,
            reason=reason.strip(),
            source="agent_added",
        )
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}", code="internal")

    # Bust the sector_map cache so the new symbol shows up immediately for
    # downstream callers (conviction validation, allocator).
    global _SECTOR_MAP_CACHE, _SECTOR_MAP_CACHED_AT
    _SECTOR_MAP_CACHE = None
    _SECTOR_MAP_CACHED_AT = 0.0

    # Auto-backfill local OHLCV caches if this symbol isn't already on some
    # other agent's active watchlist (no point re-fetching what the streamer
    # is already polling). Best-effort: a Massive failure here doesn't roll
    # back the watchlist insert — the streamer / daily ingest will fill it
    # on the next cycle.
    if os.environ.get("MASSIVE_API_KEY", "").strip():
        try:
            already_watched = await _already_watched_elsewhere(agent_name, sym)
            if not already_watched:
                bf = await _backfill_ticker_history(sym)
                result["backfill"] = bf
            else:
                result["backfill"] = {"skipped": "already on another agent's watchlist"}
        except Exception as e:
            result["backfill"] = {"error": f"{type(e).__name__}: {e}"}

    return json.dumps(result, default=str)


async def _already_watched_elsewhere(agent_name: str, symbol: str) -> bool:
    """Is this symbol active on some other agent's watchlist? If yes, the
    streamer is already polling it — skip the backfill in add_to_watchlist."""
    from db import store
    grouped = await store.load_all_watchlists()
    for a, rows in grouped.items():
        if a == agent_name:
            continue
        if any(r["symbol"] == symbol for r in rows):
            return True
    return False


@mcp.tool()
async def propose_watchlist_removal(
    agent_name: str,
    symbol: str,
    reasoning: str,
) -> str:
    """
    Propose removing a ticker from the agent_watchlist SQL table. Removal
    requires user approval via Telegram — this tool creates a pending
    proposal in the approval queue (kind="watchlist_removal") and stamps
    the row's `removal_pending` field with the proposal id.

    The user sees a Telegram message with the ticker + your reasoning;
    they reply `/y` to approve or `/n` to reject. On approval, the SQL
    row is soft-deleted (removed_at + removed_reason set) the next time
    any watchlist loader runs — usually within seconds.

    Args:
        agent_name: Your agent name.
        symbol: Ticker to remove (will be uppercased).
        reasoning: WHY this name should drop off the watchlist. The user
                   reads this reasoning to decide; be concrete (≥30 chars).
    """
    await _ensure_init_light()
    sym = symbol.strip().upper()
    if not sym:
        return _err("symbol is required")
    if not (reasoning or "").strip() or len((reasoning or "").strip()) < 30:
        return json.dumps({
            "error": "reasoning must be ≥30 chars — explain why this name "
                     "should drop off the watchlist",
        })
    from db import store
    if not await store.agent_owns_symbol_db(agent_name, sym):
        return json.dumps({
            "error": f"{sym} is not on {agent_name}'s active watchlist; nothing to remove",
        })

    from approval import proposals
    proposal = await proposals.create(
        title=f"Watchlist removal: {agent_name} → drop {sym}",
        details=(
            f"*Agent:* {agent_name}\n"
            f"*Symbol:* {sym}\n\n"
            f"*Reasoning:*\n{reasoning.strip()}"
        ),
        kind="watchlist_removal",
        payload={
            "agent_name": agent_name,
            "symbol": sym,
            "reasoning": reasoning.strip(),
        },
    )
    await store.mark_watchlist_removal_pending(agent_name, sym, proposal["id"])
    return json.dumps({
        "proposal_id": proposal["id"][:8],
        "status": proposal.get("status", "pending"),
        "agent_name": agent_name,
        "symbol": sym,
    })


@mcp.tool()
async def submit_forecast_batch(
    agent_name: str,
    forecasts: list,
) -> str:
    """
    Publish a batch of forecast rows on names from your sector universe.
    Forecasts are PROOF-OF-WORK — show your thinking across ≥20 names per
    hour, regardless of whether you take a conviction. Allocator does NOT
    directly read this table for scalar trades; convictions remain that signal.
    However, rows carrying a `distribution` payload feed the allocator's
    mixture-then-functional path (Phase E onward) and the calibration scorer.

    MULTI-HORIZON: Each symbol may appear in multiple rows under different
    horizons. The horizon enum is now:
      5m | 1h | intraday | near | far | cycle
    The same symbol at different time horizons becomes independent DB rows,
    allowing you to express "bullish next hour, neutral next week, bearish
    next quarter" cleanly without conflict.

    Args:
        agent_name: Your agent name.
        forecasts: list of dicts, each carrying:
            symbol               (required) — must be in your sector universe.
            expected_return_pct  (required) — signed % move you forecast at
                                  this specific horizon. May differ across horizons.
            likelihood           (required) — probability of hitting target, 0..1.
            time_to_target_days  (required) — horizon in days, > 0. Drives
                                  automatic horizon bucket assignment.
            method               (required) — free-text source, e.g.
                                  "BBG consensus PT", "RSI 68 + EUV TAM cut",
                                  "sentiment + macro". May differ per horizon.
            rationale            (optional) — one-line note.
            horizon              (optional) — override auto-derived bucket;
                                  one of: 5m, 1h, intraday, near, far, cycle.
            distribution         (optional) — full probabilistic forecast payload
                                  (closed schema, see
                                  meta_agent/distribution_validator.py:
                                  anchor_price, anchor_ts, axis, horizon, bins,
                                  model, model_version). Validated server-side:
                                  3–20 uniform-spaced bins with p≥1e-4 summing
                                  to 1. Required if you want this row scored by
                                  the calibration loop / consumed by the
                                  conviction-functional pipe.
            forecast_run_id      (optional) — UUID joining rows from the same
                                  model run; populated automatically when
                                  submit_conviction_from_model emits a
                                  multi-horizon batch.
            expires_in_hours     (REQUIRED, per row) — auto-expire after N
                                  hours. Range: 0.0833 (5 min) to 720
                                  (30 days). Pick to match the horizon: a
                                  5m forecast might carry 0.25h, a cycle
                                  forecast 168h. No default — each row must
                                  specify. Re-submit at each hourly review
                                  with `clear_my_forecasts` to refresh.

    Returns: {inserted, skipped_validation, ownership_errors, soft_warning}.
    Soft-warns when unique_symbols < 20 — submission still succeeds.
    """
    await _ensure_init_light()
    ok, reason = _rate_check("submit_forecast_batch")
    if not ok:
        return _err(reason)
    if not isinstance(forecasts, list) or not forecasts:
        return _err("forecasts must be a non-empty list of dicts")

    # Universe enforcement up front so the agent gets a clean rejection list
    # rather than partial inserts followed by surprise drops.
    ownership_errors: list[dict] = []
    cleaned: list[dict] = []
    for r in forecasts:
        sym = (r or {}).get("symbol")
        if not sym:
            ownership_errors.append({"row": r, "error": "missing symbol"})
            continue
        ok_sym, reason = _validate_symbol(str(sym))
        if not ok_sym:
            ownership_errors.append({"row": r, "error": f"validation: {reason}"})
            continue
        allowed, reason = await _agent_owns_symbol(agent_name, str(sym))
        if not allowed:
            ownership_errors.append({"row": r, "error": f"watchlist: {reason}"})
            continue
        cleaned.append(r)

    from db import store
    try:
        result = await store.upsert_forecasts_batch(
            agent_name=agent_name,
            rows=cleaned,
        )
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}", code="internal")

    unique_symbols = len({(r.get("symbol") or "").upper() for r in cleaned})
    payload = {
        "inserted": result["inserted"],
        "skipped_validation": result["errors"],
        "ownership_errors": ownership_errors,
    }
    if unique_symbols < 20:
        payload["soft_warning"] = (
            f"only {unique_symbols} distinct symbols; the desk policy is ≥20 per hour"
        )
    return json.dumps(payload, default=str)


@mcp.tool()
async def clear_my_forecasts(agent_name: str, horizon: str = "") -> str:
    """
    Retract this agent's currently-active forecast rows by soft-deleting them
    (sets expires_at = NOW()). Rows stay on disk so the calibration scorer can
    still grade an already-passed forecast you've retracted. Call at the start
    of each hourly review (before submit_forecast_batch) so the new batch fully
    replaces the prior hour's slate.

    Args:
        agent_name: Your agent name.
        horizon: Optional — clear only this horizon ('5m', '1h', 'intraday',
                 'near', 'far', 'cycle'). Leave empty to clear all horizons.
                 Use horizon='intraday' when you want to preserve your weekly
                 cycle forecasts while refreshing short-term views.
    """
    await _ensure_init_light()
    from db import store
    h = horizon.strip() or None
    deleted = await store.clear_agent_forecasts(agent_name, horizon=h)
    payload = {"deleted": deleted}
    if h:
        payload["horizon"] = h
    return json.dumps(payload)


@mcp.tool()
async def get_my_active_forecasts(agent_name: str, horizon: str = "") -> str:
    """
    Read this agent's currently active (non-expired) forecast rows, ordered by
    horizon bucket (intraday → near → far → cycle) then abs(forecast_score).
    Useful for hour-over-hour continuity — see what you said last hour before
    forming this hour's view.

    Args:
        agent_name: Your agent name.
        horizon: Optional filter — return only this horizon ('intraday',
                 'near', 'far', 'cycle'). Leave empty for all horizons.
    """
    await _ensure_init_light()
    from db import store
    h = horizon.strip() or None
    rows = await store.get_agent_active_forecasts(agent_name, horizon=h)
    return json.dumps({"forecasts": rows}, default=str)


@mcp.tool()
async def get_my_active_views(agent_name: str) -> str:
    """
    Read this agent's currently active (non-expired, non-flat) forecast rows
    — each carrying direction, expected_return_pct, likelihood, time_to_target_days,
    rationale. Useful for continuity: see what you said last hour before
    forming this hour's view.

    Args:
        agent_name: Your agent name.
    """
    await _ensure_init_light()
    from db import store
    rows = await store.get_agent_active_convictions(agent_name)
    return json.dumps({"views": rows}, default=str)


@mcp.tool()
async def get_consolidated_view(caller: str = "") -> str:
    """
    Cross-desk view for the allocator. Mike-only.
    Returns {symbol: {long_sum, short_sum, net, contributors: [{agent, direction,
    conviction, expected_return_pct, time_to_target_days, rationale}, ...]}}.

    Args:
        caller: Must be 'mike'.
    """
    await _ensure_init_light()
    if caller != "mike":
        return json.dumps({"error": "get_consolidated_view is mike-only", "error_code": "permission", "view": {}})
    from db import store
    view = await store.get_consolidated_view()
    return json.dumps({"view": view}, default=str)


@mcp.tool()
async def rebalance_desk(
    caller: str = "",
    dry_run: bool = True,
    gross_leverage: float = 2.0,
    max_per_symbol: float = 0.40,
    min_trade_threshold: float = 0.002,
    influence_weights: Optional[dict] = None,
) -> str:
    """
    Run the conviction-weighted allocator. Mike-only. Default dry_run=True.

    Reads every agent's active conviction views, computes signed target weights,
    diffs against current positions, and (in live mode) places delta orders.
    Writes one allocation_decision row per run; in live mode the IBKR daemon's
    fill callback writes agent_ledger LEND/RETURN events as fills land.

    Args:
        caller: Must be 'mike'.
        dry_run: If True (default), only logs proposed orders — no orders placed.
        gross_leverage: Sum of |target_weights|. 1.0 = no margin; default 2.0
                        (2x leverage; aggressive cap regime).
        max_per_symbol: Hard cap per name as fraction of NAV. Default 0.40.
        min_trade_threshold: Skip orders smaller than this fraction of NAV.
                             Default 0.002.
        influence_weights: Per-agent multiplier {agent: float}. Default all 1.0.

    Returns: JSON with target_weights, contributors, proposed_orders, decision_id.
    """
    await _ensure_init_light()
    if caller != "mike":
        return _err("rebalance_desk is mike-only")
    ok, reason = _rate_check("rebalance_desk")
    if not ok:
        return _err(reason)

    from db import store
    from meta_agent.allocator import (
        ConvictionView, compute_target_weights, diff_to_orders, net_inverse_pairs,
        classify_inverse_order_gate, apply_safety_brakes,
        enrich_views_with_mixture, use_mixture_enabled,
    )
    from data.massive_client import get_quote as _get_quote
    from ibkr.account import get_account_summary
    from approval import proposals as _approval_proposals

    # ── Step 1: execute previously-approved trade proposals ───────────────────
    # Approved early-inverse-ETF entries from earlier runs land here. Placing
    # them BEFORE the rebalance lets the in-flight reconciliation below fold
    # them into current_positions so diff_to_orders doesn't re-issue them.
    # In dry_run we only report; we don't execute.
    approved_trades_placed: list[dict] = []
    just_placed_symbols: set[str] = set()
    if not dry_run:
        for prop in _approval_proposals.list_by("trade_approval", ("approved",)):
            pay = prop.get("payload") or {}
            sym = (pay.get("vehicle") or "").upper()
            qty = int(pay.get("qty") or 0)
            if not sym or qty <= 0:
                continue
            rationales = "; ".join(
                f"{(c.get('agent') or '?')}: {(c.get('rationale') or '').strip()}"
                for c in (pay.get("contributions") or [])
            )[:200]
            try:
                res_json = await place_order(
                    agent_name="mike",
                    symbol=sym,
                    action="BUY",
                    quantity=float(qty),
                    order_type="MKT",
                    reasoning=f"[allocator+approved id={prop['id'][:8]}] {rationales}",
                )
                try:
                    res = json.loads(res_json)
                except (TypeError, ValueError):
                    res = {"raw": str(res_json)}
                _approval_proposals.mark_placed(prop["id"])
                approved_trades_placed.append({
                    "id": prop["id"][:8], "symbol": sym, "qty": qty, "result": res,
                })
                just_placed_symbols.add(sym)
            except Exception as e:
                approved_trades_placed.append({
                    "id": prop["id"][:8], "symbol": sym, "qty": qty,
                    "error": f"{type(e).__name__}: {e}",
                })

    # Load views
    rows = await store.get_active_convictions()
    views = [
        ConvictionView(
            agent_name=r["agent_name"], symbol=r["symbol"], direction=r["direction"],
            conviction=float(r["conviction"]),
            expected_return_pct=(float(r["expected_return_pct"]) if r.get("expected_return_pct") is not None else None),
            time_to_target_days=r.get("time_to_target_days"),
            rationale=r.get("rationale"),
            momentum_confirmed=r.get("momentum_confirmed"),
            stop_pct=(float(r["stop_pct"]) if r.get("stop_pct") is not None else None),
        )
        for r in rows
    ]

    # Phase-E flag-gated mixture path. When ALLOC_USE_MIXTURE=1, symbols with
    # active distributions in agent_forecast get their per-agent scalar votes
    # replaced with mixture-derived rows (one per contributor) summing to the
    # functional-applied scalar. Symbols without distributions stay on the
    # legacy scalar-sum path. mixture_report captures both for A/B logging.
    mixture_report: dict[str, dict] = {}
    if use_mixture_enabled():
        try:
            views, mixture_report = await enrich_views_with_mixture(
                views, influence_weights=influence_weights or {},
            )
        except Exception as exc:
            # Fail-open: any mixer failure falls back to scalar-sum so the desk
            # keeps trading. The error is surfaced in the response payload.
            mixture_report = {"_error": f"{type(exc).__name__}: {exc}"}

    tw = compute_target_weights(
        views,
        influence_weights=influence_weights or {},
        gross_leverage=gross_leverage,
        max_per_symbol=max_per_symbol,
        min_trade_threshold=min_trade_threshold,
    )

    # Net (long underlying + long its inverse) pairs into single positions so
    # the desk doesn't trade both sides of an offsetting pair. Reads the audited
    # agents/inverse_etf_map.yaml.
    inverse_map = _load_inverse_map()
    netted_weights, netted_contributors, netting_log = net_inverse_pairs(
        tw.weights, tw.contributors, inverse_map,
    )
    # Replace tw.weights and tw.contributors with the netted versions.
    tw.weights = netted_weights
    tw.contributors = netted_contributors

    # NAV + current positions. Mike is the ONLY path allowed to query IBKR
    # for live cash/nav; everyone else reads the kanban / nav_log mirror.
    summary = await get_account_summary()
    nav = float(summary.get("nav") or 0.0)
    cash_balance = float(summary.get("cash") or 0.0)
    positions_resp = await store.get_open_positions() if hasattr(store, "get_open_positions") else None
    # Fall back to ibkr live positions if store helper not present.
    # NOTE: ibkr.account.get_positions returns {symbol, quantity, avg_cost} —
    # no market_value, no "position" key. We compute market_value below once
    # quotes are loaded; otherwise diff_to_orders sees current_value=0 and
    # re-buys the full target every run (the bug that doubled positions).
    from ibkr.account import get_positions as _ibkr_positions, get_open_orders as _ibkr_open_orders
    pos_rows = await _ibkr_positions()
    current_positions = {
        (p.get("symbol") or "").upper(): {
            "position": float(p.get("quantity", 0) or 0),
            "market_value": 0.0,                       # filled in after quotes load
            "avg_cost": float(p.get("avg_cost", 0.0) or 0.0),
        }
        for p in (pos_rows or [])
    }

    # Partial-fill reconciliation: fold the *remaining* (unfilled) portion of any
    # working order into market_value with the correct sign so the diff doesn't
    # re-issue what's already at the broker. Without this, a BUY 100 that fills
    # 50/100 makes the next allocator run BUY another 50 on top of the resting 50.
    try:
        open_orders = await _ibkr_open_orders() or []
    except Exception as exc:
        log.warning("rebalance_desk: get_open_orders failed (%s); proceeding without in-flight reconciliation", exc)
        open_orders = []
    for oo in open_orders:
        osym = (oo.get("symbol") or "").upper()
        if not osym:
            continue
        remaining = float(oo.get("remaining") or 0.0)
        if remaining <= 0:
            continue
        # Pricing for the in-flight notional: limit price if available, else fall
        # back to last known quote for that symbol (fetched below). Use 0 here
        # and patch in after `quotes` is populated.
        side = (oo.get("action") or "").upper()
        sign = +1 if side == "BUY" else -1 if side == "SELL" else 0
        if sign == 0:
            continue
        cur = current_positions.setdefault(osym, {"position": 0, "market_value": 0.0, "avg_cost": 0.0})
        cur.setdefault("_inflight_remaining", 0.0)
        cur.setdefault("_inflight_limit_value", 0.0)
        cur["_inflight_remaining"] += sign * remaining
        lp = oo.get("limit_price")
        if lp:
            cur["_inflight_limit_value"] += sign * remaining * float(lp)

    # Quotes for every target symbol AND every currently-held symbol.
    # For negative weights, resolve to inverse-ETF (or "skip" if desk policy
    # bans the bearish vehicle); track skipped views so the allocator response
    # surfaces what conviction didn't translate into an order.
    # CASH is not present in tw.weights (compute_target_weights pops it); guard
    # anyway so any future pseudo-symbols don't try to fetch quotes or orders.
    needed_symbols = set(s for s in current_positions.keys() if s != "CASH")
    sector_map = await _load_sector_map()
    from meta_agent.allocator import resolve_bearish_vehicle
    skipped_views: list[dict] = []
    for sym, w in tw.weights.items():
        if sym.upper() == "CASH":
            continue
        if w >= 0:
            needed_symbols.add(sym.upper())
            continue
        v, mode = resolve_bearish_vehicle(sym, sector_map)
        if mode == "inverse_etf":
            needed_symbols.add(v)
        else:
            # mode == "skip": desk policy bans direct shorts. Drop the view
            # from order generation but record for transparency.
            skipped_views.append({
                "symbol": sym,
                "weight": w,
                "reason": "no inverse-ETF mapping (agent_watchlist.bearish_via); desk policy prohibits direct shorts",
                "contributors": [{"agent": a, "weight": cw} for (a, cw) in tw.contributors.get(sym, [])],
            })

    quotes: dict[str, float] = {}
    for sym in needed_symbols:
        try:
            q = await _get_quote(sym)
            quotes[sym] = float(q.get("last") or q.get("close") or 0.0)
        except Exception:
            continue

    # Now that quotes are loaded, populate each held position's market_value
    # (held qty × live quote) and fold in the in-flight remaining qty so the
    # diff against target reflects what we already own + what's already at the
    # broker. Without this, current_value == 0 and the allocator re-buys the
    # full target every run (the doubling bug observed 2026-04-27).
    for osym, cur in current_positions.items():
        held_qty = float(cur.get("position") or 0.0)
        last_px = quotes.get(osym) or quotes.get(osym.lower()) or 0.0
        held_value = held_qty * last_px
        rem = cur.pop("_inflight_remaining", 0.0)
        limit_val = cur.pop("_inflight_limit_value", 0.0)
        priced_qty = limit_val / last_px if (limit_val and last_px) else 0.0
        unpriced_qty = rem - priced_qty
        unpriced_val = unpriced_qty * last_px
        cur["market_value"] = held_value + limit_val + unpriced_val

    # Defensive auto-flat brakes. With market_value now populated we can check
    # each view's stop_pct against the position's unrealized return; any view
    # that fires its stop is downgraded to flat and we re-run the target-weight
    # pipeline so its weight drops out cleanly (without re-normalizing the
    # remaining contributors incorrectly). The first compute above is kept as
    # the seed used to build needed_symbols + fetch quotes; this re-run is the
    # authoritative tw passed to diff_to_orders.
    views_braked, brake_log = apply_safety_brakes(views, current_positions)
    if brake_log:
        tw = compute_target_weights(
            views_braked,
            influence_weights=influence_weights or {},
            gross_leverage=gross_leverage,
            max_per_symbol=max_per_symbol,
            min_trade_threshold=min_trade_threshold,
        )
        netted_weights, netted_contributors, netting_log = net_inverse_pairs(
            tw.weights, tw.contributors, inverse_map,
        )
        tw.weights = netted_weights
        tw.contributors = netted_contributors

    proposed = diff_to_orders(
        tw.weights,
        current_positions,
        quotes,
        nav=nav,
        sector_map=sector_map,
        min_trade_threshold=min_trade_threshold,
    )

    # Per-run notional cap (M6). Stop placing once the cumulative |delta_value|
    # of remaining orders would exceed risk.max_run_notional.
    #
    # Order priority: SELLs (closes) first, then BUYs (opens). Within each
    # group, largest |delta| first. This guarantees that hour-to-hour
    # reconciliation closes lapsed-conviction positions before consuming
    # budget on new opens — orphan inverse-ETF decay was the dominant loss
    # vector in early-May, where the prior "largest first" sort kept burying
    # close orders behind larger opens.
    risk_cfg = (_cfg or {}).get("risk", {})
    max_run_notional = float(risk_cfg.get("max_run_notional", 0) or 0)
    cap_dropped: list[dict] = []
    if max_run_notional > 0:
        proposed.sort(key=lambda o: (
            0 if (o.side or "").upper() == "SELL" else 1,
            -abs(float(o.delta_value or 0.0)),
        ))
        kept: list = []
        running = 0.0
        for o in proposed:
            d = abs(float(o.delta_value or 0.0))
            if running + d > max_run_notional:
                cap_dropped.append({"symbol": o.symbol, "side": o.side, "qty": o.qty,
                                     "delta_value": o.delta_value, "reason": "max_run_notional"})
                continue
            running += d
            kept.append(o)
        proposed = kept

    # Sub-10-share gate. The allocator runs unattended hourly and must NOT
    # block on Telegram mid-run, so handle expensive sub-min orders by skipping
    # them here and surfacing under `pending_user_review` for the operator to
    # approve manually via place_order. Cheap sub-min orders are dropped flat.
    from risk.checks.order_size import evaluate_min_quantity
    pending_user_review: list[dict] = []
    min_qty_dropped: list[dict] = []
    filtered: list = []
    for o in proposed:
        last_px = quotes.get(o.symbol) or quotes.get(o.symbol.lower()) or 0.0
        status, reason = evaluate_min_quantity(float(o.qty), last_px or None, _cfg)
        if status == "ok":
            filtered.append(o)
        elif status == "needs_telegram_approval":
            pending_user_review.append({
                "symbol": o.symbol, "side": o.side, "qty": o.qty,
                "last_price": last_px, "delta_value": o.delta_value,
                "rationale": o.rationale, "reason": reason,
            })
        else:  # reject
            min_qty_dropped.append({
                "symbol": o.symbol, "side": o.side, "qty": o.qty,
                "last_price": last_px, "delta_value": o.delta_value,
                "reason": reason,
            })
    proposed = filtered

    # ── Momentum-confirmed gate: partition orders by approval requirement ─────
    # BUY on a verified inverse-ETF symbol whose long-inverse contributors did
    # not ALL self-assert momentum_confirmed=True is queued for human approval
    # via Telegram instead of placed this run. Reductions and non-inverse
    # symbols pass through unconditionally. Symbols just placed from a
    # previously-approved proposal are also dropped to avoid duplicate orders
    # before IBKR's open_orders view catches up.
    pending_inverse_approvals: list[dict] = []
    quote_for_notional = lambda s: float(quotes.get(s) or quotes.get(s.lower()) or 0.0)

    def _build_payload(o, contribs) -> dict:
        inv_meta = (inverse_map.get("inverses") or {}).get(o.symbol.upper()) or {}
        last_px = quote_for_notional(o.symbol)
        return {
            "decision_id": None,  # filled in below once decision_id exists
            "vehicle": o.symbol.upper(),
            "underlying": (inv_meta.get("underlying") or "").upper(),
            "leverage": float(inv_meta.get("leverage") or 0.0),
            "qty": int(o.qty),
            "side": o.side,
            "est_notional": last_px * float(o.qty),
            "contributions": [
                {
                    "agent": v.agent_name,
                    "conviction": float(v.conviction),
                    "rationale": v.rationale or "",
                    "momentum_confirmed": v.momentum_confirmed,
                    "expected_return_pct": v.expected_return_pct,
                    "time_to_target_days": v.time_to_target_days,
                }
                for v in (contribs or [])
            ],
        }

    auto_orders: list = []
    silenced_by_recent_approval: list[dict] = []
    for o in proposed:
        if o.symbol.upper() in just_placed_symbols:
            # Approved trade just placed at the top of this run; drop to avoid
            # double-buying before IBKR's open_orders feed catches up.
            continue
        decision, contribs = classify_inverse_order_gate(
            o.symbol, o.side, views, inverse_map,
        )
        if decision == "auto":
            auto_orders.append(o)
            continue

        # decision == "gated" — would normally queue a Telegram approval.
        # Before doing that, check whether the user has ALREADY approved an
        # entry on this (vehicle, contributing-agents) tuple within the last
        # 6 hours. If yes, treat as user-blessed and place immediately —
        # otherwise the desk re-prompts every hourly review on the same
        # unconfirmed inverse-ETF view (the SOXS-from-fab loop reported
        # 2026-05-20).
        contrib_agents = {
            (v.agent_name or "").strip() for v in (contribs or []) if v.agent_name
        }
        recent_approval = (
            _approval_proposals.find_recent_approval(
                vehicle=o.symbol.upper(),
                contrib_agents=contrib_agents,
                window_seconds=6 * 3600,
            ) if contrib_agents else None
        )
        if recent_approval:
            auto_orders.append(o)
            age_h = (_time.time() - (recent_approval.get("resolved_at") or 0)) / 3600.0
            silenced_by_recent_approval.append({
                "symbol": o.symbol.upper(),
                "qty": int(o.qty),
                "side": o.side,
                "prior_proposal_id": recent_approval["id"][:8],
                "prior_approved_age_h": round(age_h, 2),
                "contrib_agents": sorted(contrib_agents),
            })
            log.info(
                "inverse-ETF gate auto-promoted via recent approval %s "
                "(age %.1fh): %s BUY %d (agents=%s)",
                recent_approval["id"][:8], age_h, o.symbol.upper(), int(o.qty),
                sorted(contrib_agents),
            )
            continue

        pending_inverse_approvals.append({
            "order": o,
            "payload": _build_payload(o, contribs),
        })
    proposed = auto_orders

    contributing_views_json = {
        sym: [{"agent": a, "weight": w} for (a, w) in tw.contributors.get(sym, [])]
        for sym in tw.weights
    }

    cash_weight = float(getattr(tw, "cash_weight", 0.0) or 0.0)
    cash_contributors_json = [{"agent": a, "weight": w} for (a, w) in getattr(tw, "cash_contributors", [])]
    decision_id = await store.record_allocation_decision(
        nav_at_decision=nav,
        target_weights=tw.weights,
        contributing_views=contributing_views_json,
        orders_placed=None,
        notes=f"{'dry_run' if dry_run else 'live'}; cash_weight={cash_weight:.4f}",
    )

    # Anchor cash + nav + positions for the deterministic kanban refresh.
    # Best-effort: a failure here doesn't block the rebalance, but the next
    # hourly refresh will fall back to fills-only / cash=0 until mike runs
    # again successfully. The positions anchor is what avoids fills-vs-IBKR
    # drift (e.g. XLF showing 90 from fills while IBKR has 105 — see DESK_POLICY §7).
    try:
        await store.record_nav_log(
            desk_nav=nav, cash_balance=cash_balance,
            decision_id=decision_id, source="mike",
        )
        anchor_positions = {
            (p.get("symbol") or "").upper(): float(p.get("quantity", 0) or 0)
            for p in (pos_rows or [])
            if (p.get("symbol") and float(p.get("quantity", 0) or 0) != 0)
        }
        await store.record_positions_anchor(
            positions=anchor_positions,
            decision_id=decision_id, source="mike",
        )
    except Exception as exc:
        log.warning("anchor writes failed (nav_log/positions_anchor): %s", exc)

    proposed_dump = [
        {"symbol": o.symbol, "side": o.side, "qty": o.qty,
         "delta_value": o.delta_value, "rationale": o.rationale}
        for o in proposed
    ]

    # ── Queue early-inverse-ETF entries as Telegram approval proposals ────────
    # Stamp decision_id, send the Telegram ping, and record one shared
    # proposal per gated order. In dry_run we surface the would-be queue but
    # don't actually send Telegram or write to the proposal store.
    pending_inverse_approvals_dump: list[dict] = []
    for entry in pending_inverse_approvals:
        o = entry["order"]
        pay = entry["payload"]
        pay["decision_id"] = decision_id
        contribs = pay.get("contributions") or []
        agent_summary = ", ".join(
            f"{c.get('agent','?')}({float(c.get('conviction') or 0):.2f})"
            for c in contribs
        ) or "no-named-contributors"
        title = (
            f"Early inverse-ETF entry: {pay['vehicle']} BUY {pay['qty']}"
            f" (~${pay['est_notional']:,.0f}) — {agent_summary}"
        )
        rationale_block = "\n".join(
            f"- {c.get('agent','?')} (conv {float(c.get('conviction') or 0):.2f}): "
            f"{(c.get('rationale') or '').strip()}"
            for c in contribs
        ) or "(no rationale provided)"
        details = (
            f"Vehicle: {pay['vehicle']} ({pay['leverage']:+.1f}x {pay['underlying']})\n"
            f"BUY qty: {pay['qty']}  Est notional: ${pay['est_notional']:,.0f}\n\n"
            f"Contributing convictions:\n{rationale_block}"
        )
        if not dry_run:
            try:
                created = await _approval_proposals.create(
                    title=title,
                    details=details,
                    kind="trade_approval",
                    payload=pay,
                )
                pending_inverse_approvals_dump.append({
                    "id": created["id"][:8], "vehicle": pay["vehicle"],
                    "qty": pay["qty"], "est_notional": pay["est_notional"],
                    "status": created.get("status", "pending"),
                })
            except Exception as e:
                pending_inverse_approvals_dump.append({
                    "vehicle": pay["vehicle"], "qty": pay["qty"],
                    "error": f"{type(e).__name__}: {e}",
                })
        else:
            pending_inverse_approvals_dump.append({
                "would_queue": True, "vehicle": pay["vehicle"],
                "qty": pay["qty"], "est_notional": pay["est_notional"],
                "title": title,
            })

    netted_pairs_dump = [
        {
            "underlying": p.underlying, "inverse": p.inverse, "leverage": p.leverage,
            "gross_underlying": p.gross_underlying, "gross_inverse": p.gross_inverse,
            "net_underlying_equiv": p.net_underlying_equiv,
            "kept": p.kept, "kept_weight": p.kept_weight,
        }
        for p in netting_log
    ]

    if dry_run:
        return json.dumps({
            "dry_run": True, "decision_id": decision_id,
            "nav": nav, "target_weights": tw.weights,
            "cash_weight": cash_weight,
            "cash_contributors": cash_contributors_json,
            "contributing_views": contributing_views_json,
            "proposed_orders": proposed_dump,
            "skipped_views": skipped_views,
            "cap_dropped": cap_dropped,
            "min_qty_dropped": min_qty_dropped,
            "pending_user_review": pending_user_review,
            "approved_trades_placed": approved_trades_placed,
            "pending_inverse_approvals": pending_inverse_approvals_dump,
            "silenced_by_recent_approval": silenced_by_recent_approval,
            "netted_pairs": netted_pairs_dump,
            "stop_pct_brakes_fired": brake_log,
            "mixture_path_enabled": use_mixture_enabled(),
            "mixture_report": mixture_report,
        })

    # Live mode: place orders. The IBKR daemon's _on_fill callback writes
    # per-agent ledger events (LEND for BUYs, RETURN for SELLs) when fills
    # land — see meta_agent/ledger_writer.py. Mike's allocator does NOT
    # write attribution rows synchronously here; that would race the fills.
    # Route orders through the risk-checked place_order MCP tool so every
    # allocator order picks up kill_switch + market_hours + order_size +
    # position_size checks plus the Telegram approval gate for orders
    # ≥ approval.threshold_usd.
    placed: list[dict] = []
    for o in proposed:
        try:
            res_json = await place_order(
                agent_name="mike",
                symbol=o.symbol,
                action=o.side,                # 'BUY' or 'SELL'
                quantity=float(o.qty),
                order_type="MKT",
                reasoning=f"allocator: {o.rationale}",
            )
            try:
                res = json.loads(res_json)
            except (TypeError, ValueError):
                res = {"raw": str(res_json)}
            placed.append({"symbol": o.symbol, "side": o.side, "qty": o.qty, "result": res})
        except Exception as e:
            placed.append({"symbol": o.symbol, "error": f"{type(e).__name__}: {e}"})

    await store.update_allocation_orders(decision_id, placed)

    # Refresh agent_state hourly snapshot so the post-rebalance read paths
    # see the new positions immediately. Best-effort: hourly cron will catch
    # up on the next tick if this fails.
    try:
        from scripts.refresh_agent_state import refresh as refresh_agent_state
        agent_state_summary = await refresh_agent_state()
    except Exception as exc:
        log.warning("agent_state refresh failed: %s", exc)
        agent_state_summary = {"error": f"{type(exc).__name__}: {exc}"}

    return json.dumps({
        "dry_run": False, "decision_id": decision_id,
        "nav": nav, "target_weights": tw.weights,
        "cash_weight": cash_weight,
        "cash_contributors": cash_contributors_json,
        "contributing_views": contributing_views_json,
        "orders_placed": placed,
        "skipped_views": skipped_views,
        "cap_dropped": cap_dropped,
        "min_qty_dropped": min_qty_dropped,
        "pending_user_review": pending_user_review,
        "approved_trades_placed": approved_trades_placed,
        "pending_inverse_approvals": pending_inverse_approvals_dump,
        "netted_pairs": netted_pairs_dump,
        "stop_pct_brakes_fired": brake_log,
        "agent_state": agent_state_summary,
        "mixture_path_enabled": use_mixture_enabled(),
        "mixture_report": mixture_report,
    }, default=str)


@mcp.tool()
async def get_agent_ledger(
    agent_name: str,
    since: Optional[str] = None,
    limit: int = 200,
) -> str:
    """
    Per-agent ledger event log (LEND / RETURN / DIVIDEND), newest-first.
    Each row is one accounting event in the agent's book — see DESK_POLICY §0.

    Use this to audit how a specific position got built up (which decisions
    drove which lent qty), or to see exactly what realized P&L came from
    which closing fill. For aggregate P&L numbers, use `get_my_pnl` /
    `get_pnl_summary`; for per-symbol current holdings, use `get_my_standing`.

    Args:
        agent_name: Whose ledger to read.
        since: ISO timestamp; if omitted returns the most recent `limit` rows.
        limit: max rows when `since` is omitted (default 200).
    """
    await _ensure_init_light()
    from db import store
    rows = await store.get_agent_ledger_events(agent_name, since=since, limit=limit)

    realized_sum = sum(
        float(r.get("realized_pnl") or 0.0)
        for r in rows
        if r.get("event") in ("RETURN", "DIVIDEND") and r.get("realized_pnl") is not None
    )
    return json.dumps({
        "events": rows,
        "summary": {
            "n_events": len(rows),
            "realized_pnl_in_window": realized_sum,
        },
    }, default=str)


@mcp.tool()
async def get_my_standing(agent_name: str, lookback_hours: int = 24) -> str:
    """
    This agent's per-symbol holdings + cumulative P&L over the last N hours
    of `agent_state` snapshots. Use in evening reviews to see your trajectory.

    Args:
        agent_name: whose standing to read.
        lookback_hours: window length (default 24h ≈ one trading day).

    Returns:
        {
          "snapshots": [<row>, ...],          # newest first
          "latest": {                         # most recent snapshot for this agent
            "snapshot_at": ..., "total_pnl": float, "realized_pnl": float,
            "unrealized_pnl": float,
            "positions": [{sym, qty, avg_cost, mark, market_value, unrealized}, ...]
          },
        }
    """
    await _ensure_init_light()
    from db import store
    rows = await store.get_agent_state_history(agent_name, lookback_hours=lookback_hours)
    latest = None
    if rows:
        r0 = rows[0]
        latest = {
            "snapshot_at": r0["snapshot_at"],
            "realized_pnl": float(r0["realized_pnl"]),
            "unrealized_pnl": float(r0["unrealized_pnl"]),
            "total_pnl": float(r0["total_pnl"]),
            "open_cost": float(r0["open_cost"]),
            "open_market_value": float(r0["open_market_value"]),
            "n_positions": int(r0["n_positions"]),
            "positions": r0["positions_json"],
        }
    return json.dumps({"snapshots": rows, "latest": latest}, default=str)


@mcp.tool()
async def get_desk_snapshot() -> str:
    """
    Cross-section: latest agent_state row per agent. Use for desk-wide
    visualisation — who holds what (with cumulative P&L) at the most recent
    hourly snapshot. For per-agent trajectory across hours, use
    `get_my_standing` with `lookback_hours=N`.
    """
    await _ensure_init_light()
    from db import store
    rows = await store.get_latest_agent_state()
    return json.dumps({
        "n_agents": len(rows),
        "rows": rows,
    }, default=str)


@mcp.tool()
async def get_archive_payload(agent_name: str, before: str) -> str:
    """
    Sector-archivist only. Aggregate everything-old for one agent up to
    (and including) `before` (ISO date, e.g. "2026-04-18"). Returns closed
    theses, expired conviction snapshots, and attributed-P&L summary so the
    archivist can draft a narrative chapter.

    Args:
        agent_name: The agent whose history to fetch.
        before:     ISO date (YYYY-MM-DD). Records on/before this date are
                    candidates for archival.
    """
    await _ensure_init_light()
    from db import store
    payload = await store.get_archive_payload(agent_name, before)
    return json.dumps(payload, default=str)


@mcp.tool()
async def write_sector_story(
    agent_name: str,
    period_start: str,
    period_end: str,
    narrative: str,
    stats: Optional[dict] = None,
) -> str:
    """
    Sector-archivist only. Persist a narrative chapter covering
    [period_start, period_end] for one agent. Replaces any existing chapter
    with the same period_end (idempotent re-runs).

    Args:
        agent_name:   Whose chapter this is.
        period_start: ISO date (YYYY-MM-DD). Inclusive lower bound.
        period_end:   ISO date (YYYY-MM-DD). Inclusive upper bound — also
                      the cut-off used by `prune_sector_history`.
        narrative:    5-12 sentence prose summary. Cite specific symbols,
                      hits, misses, regime context, and how the agent's
                      mental model evolved.
        stats:        Optional aggregates (hit_rate, top_pnl_symbol, etc.)
                      stored as JSON for later queries.
    """
    await _ensure_init_light()
    from db import store
    sid = await store.insert_sector_story(
        agent_name, period_start, period_end, narrative, stats=stats,
    )
    return json.dumps({"story_id": sid})


@mcp.tool()
async def get_sector_stories(agent_name: str, limit: int = 8) -> str:
    """
    Read the most recent narrative chapters for one agent, oldest-first.
    Each agent reads their own stories at the start of every morning review
    so the conviction stack carries continuity instead of starting blank.
    Mike may also read any agent's chapters.

    Args:
        agent_name: Whose chapters to fetch.
        limit:      Max chapters (default 8 — last ~2 months at weekly cadence).
    """
    await _ensure_init_light()
    from db import store
    rows = await store.get_sector_stories(agent_name, limit=limit)
    return json.dumps({"stories": rows}, default=str)


@mcp.tool()
async def prune_sector_history(agent_name: str, before: str) -> str:
    """
    Sector-archivist only. Delete closed theses, expired conviction rows,
    and attributed-P&L rows older than `before` for this agent. ONLY call
    this AFTER `write_sector_story` has captured the same window — the
    narrative is what survives; the raw rows are discarded.

    Args:
        agent_name: Whose old rows to prune.
        before:     ISO timestamp/date. Records on/before are deleted.
    """
    await _ensure_init_light()
    from db import store
    # Refuse to prune unless a sector_story chapter exists for this agent that
    # covers (period_end >= `before`). Prevents data loss if write_sector_story
    # was skipped or failed silently.
    stories = await store.get_sector_stories(agent_name, limit=4)
    cutoff_date = (before or "")[:10]  # YYYY-MM-DD slice
    has_chapter = any(
        str(s.get("period_end") or "")[:10] >= cutoff_date for s in (stories or [])
    )
    if not has_chapter:
        return json.dumps({
            "error": (
                f"refusing to prune {agent_name} rows on/before {before}: no sector_story "
                f"chapter found with period_end >= {cutoff_date}. "
                f"Call write_sector_story first."
            )
        })
    counts = await store.prune_archived_rows(agent_name, before)
    return json.dumps(counts)


@mcp.tool()
async def prune_global_noise(news_days: int = 14, audit_days: int = 30) -> str:
    """
    Sector-archivist only. Delete pure-noise rows that don't need narrative
    archival: stale news headlines, old audit_log entries, old tool_calls.
    News is re-fetched on demand; audit/tool_calls are ops debug trail.

    Args:
        news_days:  Delete news_items older than this many days (default 14).
        audit_days: Delete audit_log + tool_calls older than this many days
                    (default 30).
    """
    await _ensure_init_light()
    from db import store
    counts = await store.prune_global_noise(news_days=news_days, audit_days=audit_days)
    return json.dumps(counts)


# ── Desk threads board ────────────────────────────────────────────────────────
#
# Public, multi-author bulletin. Every agent reads active posts in
# `desk-announcements` via prompt injection (see agent/prompt_builder.py); they
# can also browse any other thread on demand. Posts may be made by user, agents,
# external feeds (e.g. news), or the system.

_AGENT_NAMES_CACHE: Optional[set[str]] = None


def _known_agent_names() -> set[str]:
    """Cached set of known agent names from agents/*.yaml."""
    global _AGENT_NAMES_CACHE
    if _AGENT_NAMES_CACHE is not None:
        return _AGENT_NAMES_CACHE
    try:
        from agent.agent_registry import list_agents as _list_agents
        names = {a.get("name") for a in _list_agents(enabled_only=False) if a.get("name")}
    except Exception:
        names = set()
    _AGENT_NAMES_CACHE = names
    return names


def _derive_author_kind(author: str) -> str:
    a = (author or "").strip()
    if not a:
        return "system"
    if a == "user":
        return "user"
    if a.startswith("feed:"):
        return "external_feed"
    if a in _known_agent_names():
        return "agent"
    return "system"


@mcp.tool()
async def list_threads(include_archived: bool = False) -> str:
    """
    List all threads on the desk-wide board with post counts and last activity.

    Args:
        include_archived: Include soft-deleted threads (default False).
    """
    await _ensure_init_light()
    from db import store
    rows = await store.list_threads(include_archived=include_archived)
    return json.dumps({"threads": rows}, default=str)


@mcp.tool()
async def get_thread_posts(
    thread_slug: str,
    limit: int = 20,
    before_id: Optional[int] = None,
    since_id: Optional[int] = None,
    author: Optional[str] = None,
    only_active: bool = True,
) -> str:
    """
    Read posts from one thread, newest first. Use this to browse desk-wide
    notices, peer agents' reports, news headlines, or your own past posts.

    Args:
        thread_slug: e.g. 'desk-announcements', 'mikes-morning', 'atlas-reports'
        limit: Max posts to return (1–200, default 20).
        before_id: Pagination — return posts older than this id.
        since_id: Pagination — return posts newer than this id.
        author: Filter to one author (e.g. 'atlas', 'user', 'feed:reuters').
        only_active: Hide expired posts (default True).
    """
    await _ensure_init_light()
    ok, reason = _rate_check("get_thread_posts")
    if not ok:
        return _err(reason)
    from db import store
    rows = await store.get_posts(
        thread_slug=thread_slug, limit=limit, before_id=before_id,
        since_id=since_id, author=author, only_active=only_active,
    )
    return json.dumps({"thread_slug": thread_slug, "posts": rows}, default=str)


@mcp.tool()
async def post_to_thread(
    thread_slug: str,
    author: str,
    body: str,
    title: Optional[str] = None,
    meta: Optional[dict] = None,
    expires_in_hours: Optional[float] = None,
    parent_post_id: Optional[int] = None,
) -> str:
    """
    Append a post to a thread. Use for: user announcements, agent daily/weekly
    reports, news feed entries, system events. The system derives author_kind
    from the author string ('user', recognized agent name, 'feed:*', else
    'system'). Body is capped at 8000 chars.

    Args:
        thread_slug: Target thread (must already exist).
        author: 'user' | <agent_name> | 'feed:<source>' | 'system'.
        body: Markdown-friendly. ≤8000 chars.
        title: Optional headline.
        meta: Optional structured payload (sentiment, symbols, urls).
        expires_in_hours: Auto-expire transient announcements; omit for permanent.
        parent_post_id: For threaded replies within a thread.
    """
    await _ensure_init_light()
    ok, reason = _rate_check("post_to_thread")
    if not ok:
        return _err(reason)
    ok, reason = _validate_rationale(body, max_len=8000)
    if not ok:
        return _err(f"validation: {reason}", code="validation")
    author_kind = _derive_author_kind(author)
    from db import store
    try:
        post_id = await store.post_to_thread(
            thread_slug=thread_slug, author=author, author_kind=author_kind,
            body=body, title=title, meta=meta,
            parent_post_id=parent_post_id, expires_in_hours=expires_in_hours,
        )
    except ValueError as e:
        return _err(f"validation: {e}", code="validation")
    return json.dumps({
        "post_id": post_id, "thread_slug": thread_slug,
        "author": author, "author_kind": author_kind,
    })


@mcp.tool()
async def create_thread(
    slug: str,
    title: str,
    description: Optional[str] = None,
    tags: Optional[list[str]] = None,
) -> str:
    """
    Create a new thread (or update an existing one's title/description/tags).
    Slugs are stable identifiers — use lowercase-with-dashes.

    Args:
        slug: Stable identifier (e.g. 'atlas-reports').
        title: Human-readable title.
        description: Optional explanation of what goes in this thread.
        tags: Optional list of tags for grouping.
    """
    await _ensure_init_light()
    if not slug or not title:
        return _err("slug and title are required")
    from db import store
    thread_id = await store.create_thread(
        slug=slug, title=title, description=description, tags=tags or [],
    )
    return json.dumps({"thread_id": thread_id, "slug": slug})


@mcp.tool()
async def search_posts(
    query: str,
    thread_slug: Optional[str] = None,
    author: Optional[str] = None,
    limit: int = 50,
) -> str:
    """
    Search posts by substring (ILIKE on title and body).

    Args:
        query: Substring to find. Required.
        thread_slug: Restrict to one thread.
        author: Restrict to one author.
        limit: Max results (1–200, default 50).
    """
    await _ensure_init_light()
    ok, reason = _rate_check("search_posts")
    if not ok:
        return _err(reason)
    from db import store
    rows = await store.search_posts(
        query=query, thread_slug=thread_slug, author=author, limit=limit,
    )
    return json.dumps({"query": query, "matches": rows}, default=str)


# ── ProjectParrot / Mocha briefing ────────────────────────────────────────────

@mcp.tool()
async def get_trading_briefing() -> str:
    """
    Live trading desk briefing: P&L, top positions, active conviction views, and
    per-agent attribution. Call when the user asks for a portfolio update, morning
    briefing, desk summary, or how the trading desk is performing.
    """
    await _ensure_init()
    ok, reason = _rate_check("get_trading_briefing")
    if not ok:
        return _err(reason)

    from datetime import datetime
    from ibkr.account import get_account_summary, get_positions as _get_positions
    from db import store

    # ET market day, not OS clock — see _market_date() above.
    report_date = _market_date()
    display_date = datetime.strptime(report_date, "%Y-%m-%d").strftime("%b %-d, %Y")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    def _money(v: float) -> str:
        return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"

    # Independent reads — fetch in parallel.
    # store.get_consolidated_view() is called directly (not via the mike-only
    # MCP wrapper) because we use it for read-only display, not allocation.
    # combined_pnl pulls IBKR account + positions + attribution + active
    # convictions; it's the single source of truth for "right-now" desk P&L.
    from reporting.agent_pnl import get_pnl_combined as _get_combined
    acct, positions, combined, conv_view = await asyncio.gather(
        get_account_summary(),
        _get_positions(),
        _get_combined(),
        store.get_consolidated_view(),
    )

    pnl_rows = combined["rows"]
    desk = combined["desk"]
    total_pnl = float(desk["combined_total"])
    realized_total = float(desk["realized_total"])
    unrealized_total = float(desk["unrealized_total"])
    commission_gap = 0.0  # ledger model has no commission_gap concept
    nav = float(acct.get("nav") or 0.0)
    pnl_pct = (total_pnl / nav * 100) if nav else 0.0

    best = max(pnl_rows, key=lambda r: float(r.get("total_pnl") or 0), default=None)
    worst = min(pnl_rows, key=lambda r: float(r.get("total_pnl") or 0), default=None)

    sorted_pos = sorted(
        positions,
        key=lambda p: abs(float(p.get("quantity") or 0) * float(p.get("avg_cost") or 1)),
        reverse=True,
    )[:4]
    chart_symbols = [{"symbol": p["symbol"]} for p in sorted_pos if p.get("symbol")]

    conv_items = sorted(
        [(sym, data) for sym, data in conv_view.items() if float(data.get("net") or 0) != 0],
        key=lambda x: abs(float(x[1].get("net") or 0)),
        reverse=True,
    )[:5]
    conviction_bullets = []
    for sym, data in conv_items:
        net = float(data.get("net") or 0)
        direction = "LONG" if net > 0 else "SHORT"
        conviction_bullets.append(f"{sym}: desk net {direction} (score {net:.2f})")

    sorted_agents = sorted(pnl_rows, key=lambda r: float(r.get("total_pnl") or 0), reverse=True)
    attr_lines = ["| Agent | P&L | Fills |", "|---|---|---|"]
    for r in sorted_agents:
        pnl_val = float(r.get("total_pnl") or 0)
        attr_lines.append(f"| {r['agent_name']} | {_money(pnl_val)} | {r.get('num_fills', 0)} |")
    attr_md = "\n".join(attr_lines) if len(attr_lines) > 2 else "_No attribution data yet._"

    direction_word = "up" if total_pnl >= 0 else "down"
    pnl_narration = (
        f"The desk is {direction_word} ${abs(total_pnl):,.0f} today, "
        f"a move of {pnl_pct:+.2f}% on NAV."
    )
    if best and float(best.get("total_pnl") or 0) > 0:
        pnl_narration += f" {best['agent_name'].capitalize()} leads with ${float(best['total_pnl']):,.0f}."

    pos_narration = (
        f"Here are the top {len(chart_symbols)} positions by notional size. "
        "Chart shows intraday price action."
    ) if chart_symbols else "No open positions on the desk right now."

    conv_narration = (
        f"The desk holds active conviction on {len(conv_items)} symbol{'s' if len(conv_items) != 1 else ''}. "
        "Scores represent aggregated sector-agent net conviction."
    ) if conv_items else "No active conviction views are posted right now."

    attr_narration = "Here is the P&L breakdown by sector agent for today."
    if worst and float(worst.get("total_pnl") or 0) < 0:
        attr_narration += f" {worst['agent_name'].capitalize()} is the drag at {_money(float(worst['total_pnl']))}."
    elif pnl_rows:
        attr_narration += " No agents in the red today."

    # Best/worst suppression: when every agent rounds to $0 (nothing closed AND
    # no unrealized exposure attributed), the "best agent atlas +$0 / worst
    # agent atlas +$0" footgun returns. Hide the cards in that case.
    nontrivial_rows = [r for r in pnl_rows if abs(float(r.get("total_pnl") or 0)) >= 1.0]
    show_best_worst = bool(nontrivial_rows)

    slides: list[dict] = [
        {
            "type": "stat_row",
            "narration": pnl_narration,
            "title": "Today's P&L (realized + unrealized)",
            "stats": [
                {"label": "Total P&L", "value": _money(total_pnl), "delta": f"{pnl_pct:+.2f}%"},
                {"label": "NAV", "value": f"${nav:,.0f}"},
                {"label": "Best agent",
                 "value": (f"{best['agent_name']} {_money(float(best['total_pnl']))}"
                           if (show_best_worst and best) else "—")},
                {"label": "Worst agent",
                 "value": (f"{worst['agent_name']} {_money(float(worst['total_pnl']))}"
                           if (show_best_worst and worst) else "—")},
            ],
        },
    ]

    # Reconciliation sub-row so the user can spot commission drag and orphan
    # exposure (open positions with no current conviction backing them).
    orphan = float(desk.get("orphan_unrealized") or 0.0)
    if abs(commission_gap) >= 1.0 or abs(orphan) >= 1.0:
        slides.append({
            "type": "stat_row",
            "narration": (
                f"Reconciliation: realized {_money(realized_total)}, "
                f"unrealized {_money(unrealized_total)}. "
                + (f"Commission/fees drag {_money(commission_gap)}. " if abs(commission_gap) >= 1.0 else "")
                + (f"{_money(orphan)} of unrealized has no active conviction backing it." if abs(orphan) >= 1.0 else "")
            ),
            "title": "P&L Reconciliation",
            "stats": [
                {"label": "Realized", "value": _money(realized_total)},
                {"label": "Unrealized", "value": _money(unrealized_total)},
                {"label": "Commission gap", "value": _money(commission_gap)},
                {"label": "Orphan unrealized", "value": _money(orphan)},
            ],
        })

    if chart_symbols:
        slides.append({
            "type": "multi_chart",
            "narration": pos_narration,
            "title": "Top Positions",
            "symbols": chart_symbols,
            "default_period": "1d",
        })

    if conviction_bullets:
        slides.append({
            "type": "bullets",
            "narration": conv_narration,
            "title": "Active Desk Conviction",
            "items": conviction_bullets,
        })

    slides.append({
        "type": "markdown",
        "narration": attr_narration,
        "title": "Agent Attribution",
        "content": attr_md,
    })

    payload = {
        "__panel__": "create_presentation",
        "__payload__": {
            "id": f"pres_trading_briefing_{ts}",
            "title": f"Trading Desk — {display_date}",
            "slides": slides,
        },
    }
    return json.dumps(payload)


# ── Parrot / Nori insight tools ───────────────────────────────────────────────

@mcp.tool()
async def get_position_dossier(symbol: str) -> str:
    """
    Deep view of one position the user holds: which agent put it on, the agent's
    thesis, recent fills, and net P&L on the symbol. Call when the user asks about
    ONE specific position — "tell me about LMT", "who long'ed X", "what's the
    thesis on X", "is the X thesis still good", "why did we close X".

    Args:
        symbol: Ticker to inspect, e.g. 'NVDA', 'LMT'
    """
    await _ensure_init_light()
    ok, reason = _validate_symbol(symbol)
    if not ok:
        return _err(reason)
    import db.store as store
    from reporting.agent_pnl import get_symbol_unrealized
    sym = symbol.upper()
    convictions, fills, pnl, unrealized = await asyncio.gather(
        store.get_convictions_for_symbol(sym),
        store.get_symbol_fills(sym, lookback_days=30),
        store.get_symbol_pnl_summary(sym),
        get_symbol_unrealized(sym),
    )
    realized = sum(float(r.get("attributed_pnl") or 0) for r in pnl)
    combined = realized + float(unrealized or 0.0)
    if not convictions and not fills:
        note = f"No desk exposure to {sym} — no active conviction and no fills in the last 30 days."
    else:
        long_agents  = [c["agent_name"] for c in convictions if c["direction"] == "long"]
        short_agents = [c["agent_name"] for c in convictions if c["direction"] == "short"]
        conflict = (f" NOTE: conflicting views — {long_agents} long vs {short_agents} short."
                    if long_agents and short_agents else "")
        note = (f"Desk holds {len(convictions)} active conviction(s) on {sym} "
                f"({len(long_agents)} long, {len(short_agents)} short), "
                f"{len(fills)} fill(s) in 30d, total P&L {combined:+,.2f} "
                f"(real {realized:+,.2f}, unreal {float(unrealized):+,.2f}).{conflict}")
    return json.dumps({
        "symbol": sym,
        "active_convictions": convictions,
        "recent_fills_30d": fills,
        "pnl_by_agent": pnl,
        "realized_pnl": realized,
        "unrealized_pnl": float(unrealized or 0.0),
        "total_pnl": combined,
        "analyst_note": note,
    }, default=str)


@mcp.tool()
async def get_agent_overview(agent_id: str) -> str:
    """
    Full snapshot of one sector agent — active convictions, P&L, allocation, recent
    sector view. Call when the user asks about ONE specific agent — "how is the
    atlas agent doing", "what's maya running", "show me agent X's book".

    Args:
        agent_id: Agent name, e.g. 'rex', 'atlas', 'maya'
    """
    await _ensure_init_light()
    import db.store as store
    from reporting.agent_pnl import get_pnl_combined
    (convictions, open_theses, resolutions, combined_today, pnl_week,
     allocations, digest, stories) = await asyncio.gather(
        store.get_agent_active_convictions(agent_id),
        store.get_open_theses(agent_id, limit=10),
        store.get_recent_resolutions(agent_id, limit=5),
        get_pnl_combined(agent_name=agent_id),
        store.get_pnl_summary(agent_name=agent_id, period="week"),
        store.get_allocations(),
        store.get_agent_evening_digest(agent_id),
        store.get_sector_stories(agent_id, limit=2),
    )
    alloc = next((a["allocation_pct"] for a in allocations if a["agent_name"] == agent_id), None)
    today_row = next(iter(combined_today["rows"]), None)
    t_pnl = float(today_row["total_pnl"]) if today_row else 0.0
    t_real = float(today_row["realized_pnl"]) if today_row else 0.0
    t_unreal = float(today_row["unrealized_pnl"]) if today_row else 0.0
    w_pnl = sum(r["total_pnl"] for r in pnl_week)
    note = (f"{agent_id.capitalize()} allocated {(alloc or 0)*100:.1f}% of NAV, "
            f"{len(convictions)} active conviction(s). "
            f"P&L today {t_pnl:+,.2f} (real {t_real:+,.2f}, unreal {t_unreal:+,.2f}), "
            f"week {w_pnl:+,.2f} (realized). "
            f"{len(open_theses)} open thesis(es).")
    return json.dumps({
        "agent_id": agent_id,
        "allocation_pct": alloc,
        "pnl_today": t_pnl,
        "pnl_today_realized": t_real,
        "pnl_today_unrealized": t_unreal,
        "pnl_week": w_pnl,
        "active_convictions": convictions,
        "open_theses_count": len(open_theses),
        "open_theses": open_theses,
        "recent_resolutions": resolutions,
        "last_evening_digest": digest,
        "sector_stories_recent": stories,
        "analyst_note": note,
    }, default=str)


@mcp.tool()
async def get_pnl_attribution(window: str = "today") -> str:
    """
    Symbol-level and agent-level P&L attribution. Call when the user asks WHY money
    moved — "why did I make money today", "what drove the loss", "where did the
    alpha come from", "who lost me money".

    Args:
        window: 'today', 'week', 'month', or 'YYYY-MM-DD/YYYY-MM-DD'
    """
    await _ensure_init_light()
    import db.store as store
    since, until = _parse_window(window)
    by_symbol, by_agent = await asyncio.gather(
        store.get_pnl_attribution_by_symbol(since, until),
        store.get_pnl_attribution_by_agent(since, until),
    )

    # For "today", overlay the live combined per-agent rows so the
    # by_agent table includes mark-to-market unrealized. by_symbol stays
    # historical-attribution only (unrealized has no per-symbol attribution
    # entry yet — surfaced via get_position_dossier instead).
    desk_realized = sum(r["total_pnl"] for r in by_agent)
    desk_unrealized = 0.0
    desk_combined = desk_realized
    commission_gap = 0.0
    orphan_unrealized = 0.0
    if window == "today":
        from reporting.agent_pnl import get_pnl_combined
        combined = await get_pnl_combined()
        by_agent = combined["rows"]
        desk_realized = combined["desk"]["realized_total"]
        desk_unrealized = combined["desk"]["unrealized_total"]
        desk_combined = combined["desk"]["combined_total"]

    if not by_symbol and not by_agent:
        note = f"No P&L recorded for {window}."
    else:
        top_sym   = by_symbol[0] if by_symbol else None
        top_agent = max(by_agent, key=lambda r: float(r.get("total_pnl") or 0), default=None)
        bits = [f"Desk total {desk_combined:+,.2f} over {window}"]
        if window == "today":
            bits.append(f"(real {desk_realized:+,.2f}, unreal {desk_unrealized:+,.2f})")
        if top_sym:
            bits.append(f"best symbol: {top_sym['symbol']} ({top_sym['total_pnl']:+,.2f})")
        if top_agent:
            bits.append(f"{top_agent['agent_name'].capitalize()} leads agents "
                        f"with {float(top_agent['total_pnl']):+,.2f}")
        note = ". ".join(bits) + "."
    return json.dumps({
        "window": window,
        "since": since,
        "desk_total_pnl": desk_combined,
        "desk_realized_pnl": desk_realized,
        "desk_unrealized_pnl": desk_unrealized,
        "commission_gap": commission_gap,
        "orphan_unrealized": orphan_unrealized,
        "by_symbol": by_symbol,
        "by_agent": by_agent,
        "analyst_note": note,
    }, default=str)


@mcp.tool()
async def get_trade_activity(window: str = "today") -> str:
    """
    All fills and orders in a time window, grouped by agent and symbol with
    aggregate statistics. Use to audit trading behavior or investigate
    a period of unusual activity.

    Args:
        window: 'today', 'week', 'month', or 'YYYY-MM-DD/YYYY-MM-DD'
    """
    await _ensure_init_light()
    import db.store as store
    since, until = _parse_window(window)
    fills, orders, stats = await asyncio.gather(
        store.get_fills_window(since, until),
        store.get_orders_window(since),
        store.get_fill_stats_by_agent_symbol(since, until),
    )
    total_pnl  = sum(float(f.get("realized_pnl") or 0) for f in fills)
    active_ags = len({f["agent_name"] for f in fills if f["agent_name"]})
    rejected   = [o for o in orders if o["status"] in (
        "blocked", "risk_rejected", "approval_rejected", "kill_switch_blocked")]
    note = (f"Desk executed {len(fills)} fill(s) across {active_ags} agent(s) "
            f"over {window}, realizing {total_pnl:+,.2f}."
            + (f" {len(rejected)} order(s) blocked or rejected." if rejected else ""))
    return json.dumps({
        "window": window,
        "summary": {
            "total_fills": len(fills), "total_orders": len(orders),
            "total_realized_pnl": total_pnl, "active_agents": active_ags,
            "rejected_orders": len(rejected),
        },
        "stats_by_agent_symbol": stats,
        "fills": fills,
        "orders": orders,
        "analyst_note": note,
    }, default=str)


@mcp.tool()
async def get_risk_overview(focus: str = "") -> str:
    """
    Desk-wide risk snapshot: concentration, kill-switch states, allocation,
    conflicting positions. Call when the user asks about RISK or EXPOSURE —
    "biggest risk", "how concentrated", "net beta", "factor exposure",
    "what if market drops 5%".

    Args:
        focus: Optional — 'kill_switch', 'concentration', 'conflicts',
               'allocations'. Omit for full overview.
    """
    await _ensure_init_light()
    import db.store as store
    kill_states, convictions, allocations, recent_decisions = await asyncio.gather(
        store.get_kill_switch_all_states(),
        store.get_active_convictions(),
        store.get_allocations(),
        store.get_recent_allocation_decisions(limit=3),
    )
    sym_weight: dict[str, float] = {}
    for c in convictions:
        sym_weight[c["symbol"]] = sym_weight.get(c["symbol"], 0) + abs(float(c["conviction"]))
    top_conc = sorted(sym_weight.items(), key=lambda x: x[1], reverse=True)[:8]
    sym_dirs: dict[str, set] = {}
    for c in convictions:
        sym_dirs.setdefault(c["symbol"], set()).add(c["direction"])
    conflicts = [
        {"symbol": sym, "views": [c for c in convictions if c["symbol"] == sym]}
        for sym, dirs in sym_dirs.items() if "long" in dirs and "short" in dirs
    ]
    global_killed = any(r["agent_name"] is None and r["is_active"] for r in kill_states)
    killed_agents = [r["agent_name"] for r in kill_states if r["agent_name"] and r["is_active"]]
    total_alloc   = sum(a["allocation_pct"] for a in allocations)
    note_prefix   = "KILL SWITCH ACTIVE — TRADING HALTED. " if global_killed else ""
    note = (f"{note_prefix}Kill switch: {'active' if global_killed else 'inactive'}. "
            f"Active conviction on {len(sym_weight)} symbol(s). "
            f"Total agent allocation {total_alloc*100:.0f}% of NAV."
            + (f" WARNING: {len(conflicts)} conflicting long/short pair(s): "
               f"{', '.join(c['symbol'] for c in conflicts)}." if conflicts else ""))
    return json.dumps({
        "kill_switch": {
            "global_active": global_killed,
            "killed_agents": killed_agents,
            "states": kill_states,
        },
        "conviction_concentration": [{"symbol": s, "total_conviction_weight": w}
                                      for s, w in top_conc],
        "conflicting_views": conflicts,
        "allocations": allocations,
        "total_allocation_pct": total_alloc,
        "recent_decisions": recent_decisions,
        "analyst_note": note,
    }, default=str)


@mcp.tool()
async def get_agent_disagreement() -> str:
    """
    Symbols where two agents hold opposite positions, with their competing
    rationales. Call when the user asks about INTERNAL CONFLICT — "are agents
    fighting", "what's controversial in the book", "where do strategies disagree".
    """
    await _ensure_init_light()
    import db.store as store
    view = await store.get_consolidated_view()
    disagreements = []
    for sym, data in view.items():
        if float(data.get("long_sum") or 0) > 0 and float(data.get("short_sum") or 0) > 0:
            long_contr  = [c for c in data["contributors"] if c["direction"] == "long"]
            short_contr = [c for c in data["contributors"] if c["direction"] == "short"]
            spread = abs(float(data["long_sum"]) - float(data["short_sum"]))
            disagreements.append({
                "symbol": sym,
                "long_sum": data["long_sum"],
                "short_sum": data["short_sum"],
                "net": data["net"],
                "spread": round(spread, 4),
                "long_agents": [c["agent"] for c in long_contr],
                "short_agents": [c["agent"] for c in short_contr],
                "all_views": data["contributors"],
            })
    disagreements.sort(key=lambda x: x["spread"], reverse=True)
    if not disagreements:
        note = "No agent disagreements — all active conviction views are directionally consistent."
    else:
        top = disagreements[0]
        note = (f"Desk has {len(disagreements)} disagreement(s). "
                f"Highest tension on {top['symbol']} — spread {top['spread']:.2f} "
                f"({top['long_agents']} long vs {top['short_agents']} short).")
    return json.dumps({
        "disagreement_count": len(disagreements),
        "disagreements": disagreements,
        "analyst_note": note,
    }, default=str)


@mcp.tool()
async def get_position_history(symbol: str, lookback_days: int = 30) -> str:
    """
    Historical fills + conviction arc for a symbol. Call when the user asks about
    TRACK RECORD — "has X been traded before", "what's our history with TLT",
    "how often does this setup work".

    Args:
        symbol: Ticker to inspect
        lookback_days: Calendar days back to search (default 30, capped at 90)
    """
    await _ensure_init_light()
    ok, reason = _validate_symbol(symbol)
    if not ok:
        return _err(reason)
    import db.store as store
    from reporting.agent_pnl import get_symbol_unrealized
    sym = symbol.upper()
    lookback_days = min(max(1, lookback_days), 90)
    fills, pnl_by_agent, conv_arc, current, unrealized = await asyncio.gather(
        store.get_symbol_fills(sym, lookback_days=lookback_days),
        store.get_symbol_pnl_summary(sym),
        store.get_conviction_history_for_symbol(sym, lookback_days=lookback_days),
        store.get_convictions_for_symbol(sym),
        get_symbol_unrealized(sym),
    )
    cum = 0.0
    timeline = []
    for f in reversed(fills):
        cum += float(f.get("realized_pnl") or 0)
        timeline.append({
            "date": str(f["filled_at"])[:10],
            "action": f["action"],
            "quantity": f["quantity"],
            "fill_price": f["fill_price"],
            "realized_pnl": f.get("realized_pnl"),
            "cumulative_pnl": round(cum, 2),
        })
    long_n  = sum(1 for c in current if c["direction"] == "long")
    short_n = sum(1 for c in current if c["direction"] == "short")
    bias = ("long" if long_n > short_n
            else "short" if short_n > long_n
            else "flat" if current else "no conviction")
    realized_pnl = sum(r["attributed_pnl"] for r in pnl_by_agent if r["attributed_pnl"])
    unreal = float(unrealized or 0.0)
    total_pnl = realized_pnl + unreal
    note = (f"{sym}: {len(fills)} fill(s) over {lookback_days}d, "
            f"total P&L {total_pnl:+,.2f} (real {realized_pnl:+,.2f}, unreal {unreal:+,.2f}). "
            f"Current desk bias: {bias}.")
    return json.dumps({
        "symbol": sym,
        "lookback_days": lookback_days,
        "fill_timeline": timeline,
        "pnl_by_agent": pnl_by_agent,
        "realized_pnl": realized_pnl,
        "current_unrealized_pnl": unreal,
        "total_pnl": total_pnl,
        "conviction_arc": conv_arc,
        "current_convictions": current,
        "analyst_note": note,
    }, default=str)


@mcp.tool()
async def get_changes_since(since: str = "last_day") -> str:
    """
    What changed since a checkpoint — new fills, new convictions, new theses, new
    allocations. Call when the user wants to CATCH UP — "what's new since I checked",
    "anything change overnight", "catch me up", "what did I miss".

    Args:
        since: ISO timestamp e.g. '2026-04-27T14:00:00+00:00', or shorthand:
               'last_hour', 'last_4h', 'last_day' (default)
    """
    await _ensure_init_light()
    from datetime import datetime, timedelta, timezone
    _DELTA = {"last_hour": timedelta(hours=1),
              "last_4h":   timedelta(hours=4),
              "last_day":  timedelta(days=1)}
    since_iso = ((datetime.now(timezone.utc) - _DELTA[since]).isoformat()
                 if since in _DELTA else since)
    import db.store as store
    fills, orders, convictions, theses, decisions = await asyncio.gather(
        store.get_fills_window(since_iso),
        store.get_orders_window(since_iso),
        store.get_new_convictions_since(since_iso),
        store.get_new_theses_since(since_iso),
        store.get_recent_allocation_decisions(limit=10),
    )
    decisions = [d for d in decisions if str(d.get("decided_at") or "") >= since_iso[:19]]
    n = len(fills) + len(convictions) + len(theses) + len(decisions)
    note = (f"No changes recorded since {since}." if n == 0 else
            f"{n} event(s) since {since}: {len(fills)} fill(s), "
            f"{len(convictions)} conviction update(s), "
            f"{len(theses)} new thesis entry(s), "
            f"{len(decisions)} allocation decision(s).")
    return json.dumps({
        "since": since_iso,
        "event_counts": {"fills": len(fills), "orders": len(orders),
                         "convictions": len(convictions), "theses": len(theses),
                         "decisions": len(decisions)},
        "fills": fills,
        "orders": orders,
        "new_convictions": convictions,
        "new_theses": theses,
        "allocation_decisions": decisions,
        "analyst_note": note,
    }, default=str)


@mcp.tool()
async def get_upcoming_catalysts(window: str = "today") -> str:
    """
    Forward-looking calendar — upcoming theses due, conviction expirations, fresh
    news. Call when the user asks WHAT'S COMING — "anything coming up", "what's on
    the docket", "next catalyst", "upcoming earnings".

    Args:
        window: 'today' (default), 'tomorrow', or 'YYYY-MM-DD'
    """
    await _ensure_init_light()
    from datetime import date, timedelta
    today = date.today()
    if window == "today":
        due = today.isoformat()
    elif window == "tomorrow":
        due = (today + timedelta(days=1)).isoformat()
    else:
        due = window
    import db.store as store
    theses_due, expiring, news = await asyncio.gather(
        store.get_theses_due_all_agents(due),
        store.get_convictions_expiring_soon(within_hours=8),
        store.get_recent_news(symbol=None, limit=15),
    )
    note = (f"No upcoming catalysts in the {window} window."
            if not theses_due and not expiring else
            f"{len(theses_due)} prediction(s) due by {due}. "
            f"{len(expiring)} conviction view(s) expiring within 8 hours. "
            f"{len(news)} news item(s) in feed.")
    return json.dumps({
        "window": window,
        "due_date": due,
        "predictions_due": theses_due,
        "convictions_expiring_8h": expiring,
        "recent_news": news,
        "analyst_note": note,
    }, default=str)


@mcp.tool()
async def get_manual_overrides(window: str = "today") -> str:
    """
    Trades placed outside the conviction allocator (manual overrides). Call when
    the user asks if they DID SOMETHING DUMB — "did I override anything",
    "manual trades", "where did discretion help or hurt".

    Args:
        window: 'today', 'week', 'month', or 'YYYY-MM-DD/YYYY-MM-DD'
    """
    await _ensure_init_light()
    import db.store as store
    since, until = _parse_window(window)
    fills = await store.get_unattributed_fills(since, until)
    by_agent: dict[str, list] = {}
    for f in fills:
        by_agent.setdefault(f["agent_name"] or "unknown", []).append(f)
    notional = sum(abs((f["quantity"] or 0) * (f["fill_price"] or 0)) for f in fills)
    realized = sum(float(f.get("realized_pnl") or 0) for f in fills)
    note = (f"No manual overrides in {window} — all fills have conviction attribution."
            if not fills else
            f"{len(fills)} unattributed fill(s) in {window}, "
            f"notional {notional:,.0f}, P&L impact {realized:+,.2f}. "
            "These were not conviction-driven.")
    return json.dumps({
        "window": window,
        "unattributed_fill_count": len(fills),
        "total_notional": notional,
        "realized_pnl": realized,
        "by_agent": by_agent,
        "fills": fills,
        "analyst_note": note,
    }, default=str)


if __name__ == "__main__":
    mcp.run(transport="stdio")
