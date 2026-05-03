"""Per-agent P&L reporting — reads `agent_state` (the hourly snapshot) and
`agent_ledger` (the event log). No IBKR call, no LLM.

Replaces the legacy `combined_pnl.py` + `kanban_pnl.py`. The new model is
clean: every snapshot row carries cumulative `realized_pnl + unrealized_pnl
= total_pnl`, so day-over-day P&L is just a delta of `total_pnl`.

Public entry points:
- `get_pnl_combined(agent_name=None)` — current latest snapshot per agent.
  Same shape as the old combined_pnl for backwards-compat callers.
- `get_pnl_windows(agent_name=None)` — windowed deltas (1d / WTD / 1m / 3m).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


# ── Current combined P&L (latest snapshot per agent) ────────────────────────

async def get_pnl_combined(
    agent_name: Optional[str] = None,
) -> dict:
    """Return the latest agent_state row per agent in the legacy combined_pnl
    response shape so existing callers (MCP tools, charts) don't break.

    Output:
        {
          "rows": [
            {"agent_name", "realized_pnl", "unrealized_pnl", "total_pnl",
             "open_cost", "open_market_value", "n_positions", "snapshot_at"},
            ...
          ],
          "desk": {
            "realized_total", "unrealized_total", "combined_total",
            "n_agents", "snapshot_at",
          },
        }

    `snapshot_at` reflects the freshness of the underlying data — refreshed
    hourly by scripts/refresh_agent_state.py and on every mike rebalance.
    """
    from db import store as _store
    states = await _store.get_latest_agent_state(agent_name=agent_name)

    rows = []
    realized_total = 0.0
    unrealized_total = 0.0
    latest_snap = None
    for s in states:
        rows.append({
            "agent_name": s["agent_name"],
            "realized_pnl": float(s["realized_pnl"]),
            "unrealized_pnl": float(s["unrealized_pnl"]),
            "total_pnl": float(s["total_pnl"]),
            "open_cost": float(s["open_cost"]),
            "open_market_value": float(s["open_market_value"]),
            "n_positions": int(s["n_positions"]),
            "snapshot_at": s["snapshot_at"].isoformat() if s.get("snapshot_at") else None,
        })
        realized_total += float(s["realized_pnl"])
        unrealized_total += float(s["unrealized_pnl"])
        if latest_snap is None or (s.get("snapshot_at") and s["snapshot_at"] > latest_snap):
            latest_snap = s["snapshot_at"]
    rows.sort(key=lambda r: r["total_pnl"], reverse=True)
    return {
        "rows": rows,
        "desk": {
            "realized_total": realized_total,
            "unrealized_total": unrealized_total,
            "combined_total": realized_total + unrealized_total,
            "n_agents": len(rows),
            "snapshot_at": latest_snap.isoformat() if latest_snap else None,
        },
    }


# ── Windowed P&L (1d / WTD / 1m / 3m) ───────────────────────────────────────

def _window_starts(t_now: datetime) -> dict[str, datetime]:
    """Return {key: window_start_ts}. Anchored at the current snapshot's
    timestamp so windows match data, not wall clock."""
    week_start = (t_now - timedelta(days=t_now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return {
        "1d":  t_now - timedelta(days=1),
        "wtd": week_start,
        "1m":  t_now - timedelta(days=30),
        "3m":  t_now - timedelta(days=90),
    }


async def _total_pnl_at(conn, at: datetime, agent_filter: Optional[str] = None) -> dict[str, float]:
    """Return {agent_name: total_pnl} from the latest agent_state row
    at-or-before `at`. Empty dict if no snapshot exists that far back."""
    where_agent = "" if agent_filter is None else "AND agent_name = $2"
    args = [at] if agent_filter is None else [at, agent_filter]
    rows = await conn.fetch(
        f"""SELECT DISTINCT ON (agent_name)
                   agent_name, total_pnl::float8 AS total_pnl
            FROM agent_state
            WHERE snapshot_at <= $1 {where_agent}
            ORDER BY agent_name, snapshot_at DESC""",
        *args,
    )
    return {r["agent_name"]: float(r["total_pnl"]) for r in rows}


async def get_pnl_windows(
    agent_name: Optional[str] = None,
) -> dict:
    """Per-agent windowed P&L computed from `agent_state.total_pnl` deltas.

    For each window: pnl_usd = total_pnl(t_now) - total_pnl(t_window_start),
    where t_window_start resolves to the latest snapshot at-or-before the
    calendar window start. Windows with no snapshot returning None.

    Because total_pnl is cumulative-since-inception, deltas are clean —
    no settlement noise, no cash-attribution artifacts, no qty-source drift.
    """
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        bounds = await conn.fetchrow(
            "SELECT MAX(snapshot_at) AS latest, MIN(snapshot_at) AS earliest FROM agent_state"
        )
        if bounds is None or bounds["latest"] is None:
            return {"anchor": None, "windows": {}, "by_agent": {}, "desk": {}}

        t_now: datetime = bounds["latest"]
        t_earliest: datetime = bounds["earliest"]
        windows = _window_starts(t_now)

        now_pnl = await _total_pnl_at(conn, t_now, agent_filter=agent_name)
        then_pnl: dict[str, dict[str, float]] = {}
        availability: dict[str, dict] = {}
        for key, t_start in windows.items():
            available = t_start >= t_earliest
            availability[key] = {
                "start_at": t_start.astimezone(timezone.utc).isoformat(),
                "available": available,
            }
            then_pnl[key] = (
                await _total_pnl_at(conn, t_start, agent_filter=agent_name)
                if available else {}
            )

    def _delta(now_v: float, then_v: Optional[float]) -> dict:
        if then_v is None:
            return {"pnl_usd": None, "total_then": None}
        return {"pnl_usd": now_v - then_v, "total_then": then_v}

    by_agent: dict[str, dict] = {}
    for agent in sorted(now_pnl.keys()):
        v_now = now_pnl[agent]
        row = {"total_pnl_now": v_now}
        for key in ("1d", "wtd", "1m", "3m"):
            row[key] = _delta(v_now, then_pnl[key].get(agent)) if availability[key]["available"] else _delta(v_now, None)
        by_agent[agent] = row

    desk_now = sum(now_pnl.values())
    desk: dict = {"total_pnl_now": desk_now}
    for key in ("1d", "wtd", "1m", "3m"):
        if not availability[key]["available"] or not then_pnl[key]:
            desk[key] = _delta(desk_now, None)
        else:
            desk[key] = _delta(desk_now, sum(then_pnl[key].values()))

    return {
        "anchor": {
            "snapshot_at": t_now.astimezone(timezone.utc).isoformat(),
            "earliest_snapshot_at": t_earliest.astimezone(timezone.utc).isoformat(),
        },
        "windows": availability,
        "by_agent": by_agent,
        "desk": desk,
    }


# ── Helpers for tools that want a single number ─────────────────────────────

async def get_symbol_unrealized(symbol: str) -> float:
    """Cross-agent attributed unrealized for one symbol — sum the
    per-symbol entries inside each agent's positions_json. Returns 0 if
    not held."""
    from db import store as _store
    states = await _store.get_latest_agent_state()
    sym_u = symbol.upper()
    total = 0.0
    for s in states:
        for p in (s.get("positions_json") or []):
            if str(p.get("sym", "")).upper() == sym_u:
                total += float(p.get("unrealized") or 0.0)
    return total
