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

# Heavy imports go here at module load, BEFORE the asyncio event loop starts.
# Deferring these to first tool call deadlocks the Windows ProactorEventLoop.
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
            await send_message(f"⚠️ *IBKR daemon unreachable*\n`{type(exc).__name__}: {exc}`\nTool call aborted. Check ibkr-daemon.service / IB Gateway.")
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
    from meta_agent.allocation_manager import get_effective_allocation

    agent_cfg = load_agent(agent_name)
    # Live USD allocation = pct × current NAV (computed inside get_effective_allocation).
    allocation = await get_effective_allocation(agent_name)
    context = await build_context_message(agent_cfg, "context")
    strategy = build_system_prompt(agent_cfg, _cfg, allocation_override=allocation)
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
        return json.dumps({"error": f"validation: {reason}"})
    ok, reason = _rate_check("get_quote")
    if not ok:
        return json.dumps({"error": reason})
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
        return json.dumps({"error": f"validation: {reason}"})
    ok, reason = _rate_check("get_bars")
    if not ok:
        return json.dumps({"error": reason})
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
        return json.dumps({"error": f"validation: {reason}"})
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
    """Check whether the kill switch is active (globally or per agent)."""
    await _ensure_init_light()
    import db.store as store
    global_killed = await store.is_killed()
    from agent.agent_registry import list_agents
    agents = list_agents(enabled_only=False)
    per_agent = {}
    for a in agents:
        per_agent[a["name"]] = await store.is_killed(agent_name=a["name"])
    return json.dumps({"global_kill": global_killed, "per_agent": per_agent})


@mcp.tool()
async def activate_kill_switch(reason: str, agent_name: Optional[str] = None) -> str:
    """
    Activate the kill switch to halt trading.

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
        await send_message(f"🛑 *Kill switch activated* ({scope})\nReason: {reason}")
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


# ── Telegram / proposals ──────────────────────────────────────────────────────

@mcp.tool()
async def send_telegram_update(text: str) -> str:
    """
    Send a plain status message to the user via Telegram.
    Use for the hourly summary ping. Does NOT require a reply.

    Args:
        text: Markdown-formatted message body.
    """
    await _ensure_init_light()
    ok, reason = _rate_check("send_telegram_update")
    if not ok:
        return json.dumps({"sent": False, "error": reason})
    from approval.telegram import send_message
    result = await send_message(text)
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


@mcp.tool()
async def process_telegram_inbox() -> str:
    """
    Poll Telegram for new y/n replies, resolve matching proposals, and re-ping
    any pending proposals older than 5 minutes since last ping.

    Call this at the start of every routine (hourly review and 5-min nudge).
    Returns which proposals got resolved and how many were nudged.
    """
    await _ensure_init_light()
    from approval import proposals
    return json.dumps(await proposals.process_inbox())


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
    rex_guidance: Optional[str] = None,
    maya_guidance: Optional[str] = None,
    atlas_guidance: Optional[str] = None,
    titan_guidance: Optional[str] = None,
    sector_rotation: Optional[str] = None,
    overnight_notes: Optional[str] = None,
) -> str:
    """
    Persist Mike's market analysis for the given date. Writes TWO files:
    - YYYY-MM-DD.txt — full free-form analysis (appended, with UTC separator)
    - YYYY-MM-DD.json — structured per-agent sections (overwritten each call)

    Both morning and midday calls update the JSON. Traders read the JSON per-agent
    via get_mike_analysis(agent_name=...) so they only see their own guidance.

    Args:
        analysis: Full analysis text (markdown). Always required — this is the
            human-readable record.
        date: 'today' (default, market-anchored to America/New_York) or 'YYYY-MM-DD'.
        regime: One of 'BULLISH', 'BEARISH', 'NEUTRAL', 'TRANSITIONAL'. Required for
            the first write of the day; optional for updates.
        risk_tone: One-sentence summary of today's risk appetite.
        rex_guidance / maya_guidance / atlas_guidance / titan_guidance: Per-trader
            directives. Each trader sees only their own section in their context.
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
            "rex_guidance": rex_guidance,
            "maya_guidance": maya_guidance,
            "atlas_guidance": atlas_guidance,
            "titan_guidance": titan_guidance,
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
    Retrieve Mike's market analysis. If `agent_name` is one of rex/maya/atlas/titan,
    returns only that trader's section + regime + risk_tone (compact view).
    Otherwise returns the full structured JSON + full text.

    Returns an advisory message if Mike hasn't written for this date yet.

    Args:
        date: 'today' (default, America/New_York) or 'YYYY-MM-DD'.
        agent_name: Optional — 'rex', 'maya', 'atlas', 'titan' for per-agent view.
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
    """
    await _ensure_init_light()
    from db import store
    thesis_id = await store.record_thesis(
        agent_name=agent_name,
        kind=kind,
        title=title,
        body=body,
        verify_by=verify_by,
        parent_id=parent_id,
        market_snapshot=market_snapshot,
    )
    return json.dumps({"thesis_id": thesis_id})


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
        return json.dumps({"error": "get_all_journals is mike-only", "journals": {}})
    from db import store
    return json.dumps({"journals": await store.get_all_open_theses()}, default=str)


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
        return json.dumps({"error": "list_open_tool_gaps is mike-only", "gaps": []})
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
        return json.dumps({"error": "update_tool_gap_status is mike-only", "updated": False})
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
async def send_telegram_chart(image_path: str, caption: Optional[str] = None) -> str:
    """
    Send an image (PNG/JPG) to Telegram via sendPhoto. Use for end-of-day chart digests.

    Args:
        image_path: Path to image file (relative to repo root or absolute).
        caption: Optional caption text, <1024 chars. Plain text is safest; Telegram's
                 Markdown parsing is finicky with underscores and special chars.
    """
    await _ensure_init_light()
    from approval.telegram import send_photo
    result = await send_photo(image_path, caption)
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
        return json.dumps({"error": stderr.decode().strip()})
    return json.dumps({"chart_path": stdout.decode().strip()})


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
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
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
        return json.dumps({"error": "invalid agent_name or model_name (must match [a-z][a-z0-9_]{0,31})"})

    module_path = Path("agents") / agent_name / "models" / f"{model_name}.py"
    try:
        agents_root = Path("agents").resolve()
        resolved = module_path.resolve()
        if agents_root not in resolved.parents:
            return json.dumps({"error": "model path escapes agents/ tree"})
    except (OSError, ValueError):
        return json.dumps({"error": "invalid model path"})
    if not module_path.exists():
        return json.dumps({"error": f"model not found: {module_path}"})

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
        return json.dumps({"error": f"{type(e).__name__}: {e}", "model": model_name})

    return json.dumps({"model": model_name, "symbol": symbol, "result": result}, default=str)


# ── Conviction views (sector-shard architecture) ─────────────────────────────
#
# Sector agents publish signed conviction views per symbol; Mike (the allocator)
# reads the consolidated view and rebalances the desk. Agents no longer place
# orders directly — see place_order's gate (Stage 3).

_SECTOR_MAP_CACHE: Optional[dict] = None
_SECTOR_MAP_MTIME: float = 0.0

_INVERSE_MAP_CACHE: Optional[dict] = None
_INVERSE_MAP_MTIME: float = 0.0


def _load_sector_map() -> dict:
    """Load agents/sector_map.yaml; cache by mtime so edits pick up without restart."""
    global _SECTOR_MAP_CACHE, _SECTOR_MAP_MTIME
    path = Path("agents") / "sector_map.yaml"
    if not path.exists():
        return {}
    mtime = path.stat().st_mtime
    if _SECTOR_MAP_CACHE is None or mtime != _SECTOR_MAP_MTIME:
        with open(path, "r", encoding="utf-8") as f:
            _SECTOR_MAP_CACHE = yaml.safe_load(f) or {}
        _SECTOR_MAP_MTIME = mtime
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


def _validate_rationale(rationale: str, max_len: int = 512) -> tuple[bool, str]:
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


def _agent_owns_symbol(agent_name: str, symbol: str) -> tuple[bool, str]:
    """Returns (allowed, reason). Mike may submit views on any symbol (tactical hedges).
    Sector agents may submit on (a) any symbol in their universe or (b) any verified
    inverse ETF from agents/inverse_etf_map.yaml — the desk's NO-DIRECT-SHORTS policy
    routes bearish convictions through long-on-inverse, so the inverse catalog is
    universe-agnostic."""
    if agent_name == "mike":
        return True, ""
    sym = symbol.upper()
    # CASH is a reserved pseudo-symbol — every agent may submit cash conviction.
    if sym == "CASH":
        return True, ""
    smap = _load_sector_map()
    agents = (smap or {}).get("agents") or {}
    spec = agents.get(agent_name)
    if spec is None:
        return False, f"unknown agent: {agent_name}"
    universe = spec.get("universe") or {}
    if sym in {s.upper() for s in universe}:
        return True, ""
    inverse_map = _load_inverse_map() or {}
    inverses = inverse_map.get("inverses") or {}
    entry = inverses.get(sym) or inverses.get(symbol)
    if entry and entry.get("verified") is True:
        return True, ""
    return False, f"{sym} is not in {agent_name}'s sector universe and not a verified inverse ETF (see agents/sector_map.yaml + agents/inverse_etf_map.yaml)"


@mcp.tool()
async def submit_conviction_view(
    agent_name: str,
    symbol: str,
    direction: str,
    conviction: float,
    rationale: str,
    expected_return_pct: Optional[float] = None,
    time_to_target_days: Optional[int] = None,
    model_inputs: Optional[dict] = None,
    expires_in_hours: int = 4,
) -> str:
    """
    Publish a signed conviction view on one symbol. Upserts on (agent_name, symbol)
    so calling again replaces the prior view. Mike reads these to size the desk.

    Args:
        agent_name: Your agent name (e.g. 'rex', 'semi', 'atlas').
        symbol: Ticker, will be uppercased.
        direction: 'long' | 'short' | 'flat'. 'flat' must have conviction == 0.
        conviction: Positive float ≈ E[return] / time_to_target_days. Higher = stronger.
                    Use your own forecast formula; Cassidy reviews calibration in evening.
        rationale: 1–2 sentence why (audit trail).
        expected_return_pct: Your forecast (informational, used by calibration tracker).
        time_to_target_days: Your horizon (informational).
        model_inputs: Raw quant model output for replay (optional dict).
        expires_in_hours: Auto-expire after N hours (default 4). Re-submit to refresh.
    """
    await _ensure_init_light()
    ok, reason = _validate_symbol(symbol)
    if not ok:
        return json.dumps({"error": f"validation: {reason}"})
    ok, reason = _validate_rationale(rationale)
    if not ok:
        return json.dumps({"error": f"validation: {reason}"})
    ok, reason = _rate_check("submit_conviction_view")
    if not ok:
        return json.dumps({"error": reason})
    if symbol.upper() == "CASH" and direction != "long":
        return json.dumps({"error": "CASH conviction must be direction='long' (cash reserve, not margin)"})
    allowed, reason = _agent_owns_symbol(agent_name, symbol)
    if not allowed:
        return json.dumps({"error": f"sector_map: {reason}"})
    from db import store
    try:
        view_id = await store.upsert_conviction(
            agent_name=agent_name,
            symbol=symbol,
            direction=direction,
            conviction=conviction,
            expected_return_pct=expected_return_pct,
            time_to_target_days=time_to_target_days,
            rationale=rationale,
            model_inputs=model_inputs,
            expires_in_hours=expires_in_hours,
        )
        return json.dumps({"view_id": view_id, "symbol": symbol.upper(), "direction": direction})
    except (ValueError, AssertionError) as e:
        return json.dumps({"error": f"validation: {e}"})


@mcp.tool()
async def clear_my_views(agent_name: str) -> str:
    """
    Drop all of this agent's conviction rows. Call at start of each review so the
    new slate fully replaces the old one (rather than mixing stale + fresh).

    Args:
        agent_name: Your agent name.
    """
    await _ensure_init_light()
    from db import store
    deleted = await store.clear_agent_convictions(agent_name)
    return json.dumps({"deleted": deleted})


@mcp.tool()
async def get_my_active_views(agent_name: str) -> str:
    """
    Read this agent's currently active (non-expired, non-flat) conviction rows.
    Useful for continuity: see what you said last hour before forming this hour's view.

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
        return json.dumps({"error": "get_consolidated_view is mike-only", "view": {}})
    from db import store
    view = await store.get_consolidated_view()
    return json.dumps({"view": view}, default=str)


@mcp.tool()
async def rebalance_desk(
    caller: str = "",
    dry_run: bool = True,
    gross_leverage: float = 1.0,
    max_per_symbol: float = 0.20,
    min_trade_threshold: float = 0.005,
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
        gross_leverage: Sum of |target_weights|. 1.0 = no margin.
        max_per_symbol: Hard cap per name as fraction of NAV.
        min_trade_threshold: Skip orders smaller than this fraction of NAV.
        influence_weights: Per-agent multiplier {agent: float}. Default all 1.0.

    Returns: JSON with target_weights, contributors, proposed_orders, decision_id.
    """
    await _ensure_init_light()
    if caller != "mike":
        return json.dumps({"error": "rebalance_desk is mike-only"})
    ok, reason = _rate_check("rebalance_desk")
    if not ok:
        return json.dumps({"error": reason})

    from db import store
    from meta_agent.allocator import (
        ConvictionView, compute_target_weights, diff_to_orders, net_inverse_pairs,
    )
    from data.massive_client import get_quote as _get_quote
    from ibkr.account import get_account_summary

    # Load views
    rows = await store.get_active_convictions()
    views = [
        ConvictionView(
            agent_name=r["agent_name"], symbol=r["symbol"], direction=r["direction"],
            conviction=float(r["conviction"]),
            expected_return_pct=(float(r["expected_return_pct"]) if r.get("expected_return_pct") is not None else None),
            time_to_target_days=r.get("time_to_target_days"),
            rationale=r.get("rationale"),
        )
        for r in rows
    ]

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
    sector_map = _load_sector_map()
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
                "reason": "no inverse-ETF mapping in sector_map.yaml; desk policy prohibits direct shorts",
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

    proposed = diff_to_orders(
        tw.weights,
        current_positions,
        quotes,
        nav=nav,
        sector_map=sector_map,
        min_trade_threshold=min_trade_threshold,
    )

    # Per-run notional cap (M6). Stop placing once the cumulative |delta_value|
    # of remaining orders would exceed risk.max_run_notional. Larger orders
    # placed first (by abs delta_value) so the highest-conviction trades land
    # before the cap clamps.
    risk_cfg = (_cfg or {}).get("risk", {})
    max_run_notional = float(risk_cfg.get("max_run_notional", 0) or 0)
    cap_dropped: list[dict] = []
    if max_run_notional > 0:
        proposed.sort(key=lambda o: abs(o.delta_value), reverse=True)
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
            "netted_pairs": netted_pairs_dump,
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
        "netted_pairs": netted_pairs_dump,
        "agent_state": agent_state_summary,
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
        return json.dumps({"error": reason})
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
        return json.dumps({"error": reason})
    ok, reason = _validate_rationale(body, max_len=8000)
    if not ok:
        return json.dumps({"error": f"validation: {reason}"})
    author_kind = _derive_author_kind(author)
    from db import store
    try:
        post_id = await store.post_to_thread(
            thread_slug=thread_slug, author=author, author_kind=author_kind,
            body=body, title=title, meta=meta,
            parent_post_id=parent_post_id, expires_in_hours=expires_in_hours,
        )
    except ValueError as e:
        return json.dumps({"error": f"validation: {e}"})
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
        return json.dumps({"error": "slug and title are required"})
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
        return json.dumps({"error": reason})
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
        return json.dumps({"error": reason})

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
        return json.dumps({"error": reason})
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
        return json.dumps({"error": reason})
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
