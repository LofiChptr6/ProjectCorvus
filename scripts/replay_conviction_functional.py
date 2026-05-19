#!/usr/bin/env python3
"""Replay-harness: simulate one (or all) conviction functionals over a
historical window of stored distributions and report aggregate skill.

For each resolved forecast row that carries a `distribution`:
  1. Apply the named functional → scalar in [0, 1]
  2. Compute per-row P&L = sign(E[r]) · scalar · realized_return_pct
  3. Aggregate by trading day into an equity curve
  4. Report Sharpe, max-DD, win-rate, total return per (functional × horizon)

Designed to let you A/B "would functional X have made more money than
functional Y on the same beliefs?". No allocator state is recreated — this is
a per-row attribution simulation, not a paper-trading sim. It rewards
functionals that put more weight on rows that turned out to be right.

Usage:
    python scripts/replay_conviction_functional.py \\
        --functional expected_return --start 2026-05-01 --end 2026-05-16
    python scripts/replay_conviction_functional.py \\
        --all-functionals --start 2026-05-01 --end 2026-05-16
    python scripts/replay_conviction_functional.py \\
        --all-functionals --agent atlas --horizon 1h --since-days 14

Exit codes:
    0  ran successfully
    1  unexpected runtime error
    2  no qualifying rows in window
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

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

log = logging.getLogger("replay_functional")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--functional", help="Functional name from the registry. "
                                         "Required unless --all-functionals.")
    p.add_argument("--all-functionals", action="store_true",
                   help="Run every registered functional and compare.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--start", help="ISO date — inclusive (e.g. 2026-05-01).")
    g.add_argument("--since-days", type=int, help="Window = last N days.")
    p.add_argument("--end", help="ISO date — inclusive. Default: today.")
    p.add_argument("--agent", help="Filter to one agent.")
    p.add_argument("--horizon", help="Filter to one horizon (5m, 1h, 1d, 1w, "
                                      "intraday, near, far, cycle).")
    p.add_argument("--include-synthetic", action="store_true",
                   help="Include rows where distribution.model='synthetic' "
                        "(defaults to excluding them — they corrupt A/B).")
    p.add_argument("--baseline-scalar", action="store_true",
                   help="Also include a 'legacy-scalar' baseline that uses the "
                        "row's existing scalar conviction (read from "
                        "agent_conviction at the same submitted_at).")
    return p.parse_args()


def _resolve_window(args: argparse.Namespace) -> tuple[date, date]:
    if args.since_days:
        end = date.today()
        start = end - timedelta(days=args.since_days)
        return start, end
    if not args.start:
        raise SystemExit("must pass --start or --since-days")
    start = datetime.fromisoformat(args.start).date()
    end = datetime.fromisoformat(args.end).date() if args.end else date.today()
    return start, end


async def _fetch_rows(start: date, end: date, agent: str | None,
                      horizon: str | None, include_synthetic: bool) -> list[dict]:
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, agent_name, symbol, horizon, time_to_target_days,
                   expected_return_pct, realized_return_pct,
                   distribution, submitted_at, resolved_at
            FROM agent_forecast
            WHERE distribution IS NOT NULL
              AND realized_return_pct IS NOT NULL
              AND resolved_at IS NOT NULL
              AND submitted_at::date BETWEEN $1 AND $2
              AND ($3::text IS NULL OR agent_name = $3)
              AND ($4::text IS NULL OR horizon = $4)
            ORDER BY submitted_at
            """,
            start, end, agent, horizon,
        )
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        dist = d["distribution"]
        if isinstance(dist, str):
            try:
                dist = json.loads(dist)
            except json.JSONDecodeError:
                continue
        if not include_synthetic and dist.get("model") == "synthetic":
            continue
        d["distribution"] = dist
        out.append(d)
    return out


def _functional_pnls(rows: list[dict], functional_name: str) -> list[dict]:
    """Return per-row trade-attribution dicts under the given functional."""
    from meta_agent import conviction_functionals
    out: list[dict] = []
    for r in rows:
        dist = r["distribution"]
        ttd = max(float(r["time_to_target_days"] or 1), 1.0)
        try:
            scalar = conviction_functionals.run(functional_name, dist, ttd)
        except KeyError:
            raise SystemExit(f"unknown functional {functional_name!r}")
        # Direction from sign of E[r] under the distribution (mirrors the
        # runner's path); fall back to expected_return_pct sign.
        xs = [float(b["x"]) for b in dist.get("bins") or []]
        ps = [float(b["p"]) for b in dist.get("bins") or []]
        if xs and ps:
            mu = sum(x * p for x, p in zip(xs, ps))
        else:
            mu = float(r["expected_return_pct"] or 0)
        sign = 1.0 if mu > 0 else (-1.0 if mu < 0 else 0.0)
        realized = float(r["realized_return_pct"])
        pnl = sign * scalar * realized
        out.append({
            "submitted_at": r["submitted_at"],
            "trade_date":   r["submitted_at"].date(),
            "agent_name":   r["agent_name"],
            "horizon":      r["horizon"],
            "scalar":       scalar,
            "sign":         sign,
            "realized":     realized,
            "pnl":          pnl,
        })
    return out


def _baseline_pnls(rows: list[dict]) -> list[dict]:
    """Per-row P&L using the row's stored scalar (expected_return_pct *
    likelihood / time_to_target_days · sign(E[r])). Approximates "what the
    legacy non-distribution path would have produced" without joining
    agent_conviction historically."""
    out: list[dict] = []
    for r in rows:
        dist = r["distribution"]
        xs = [float(b["x"]) for b in dist.get("bins") or []]
        ps = [float(b["p"]) for b in dist.get("bins") or []]
        mu = sum(x * p for x, p in zip(xs, ps)) if xs and ps else float(r["expected_return_pct"] or 0)
        sign = 1.0 if mu > 0 else (-1.0 if mu < 0 else 0.0)
        # Use the legacy forecast_score-style scalar: |E[r]| / max(ttd, 1).
        ttd = max(float(r["time_to_target_days"] or 1), 1.0)
        scalar = min(abs(mu) / ttd, 1.0)
        realized = float(r["realized_return_pct"])
        out.append({
            "trade_date": r["submitted_at"].date(),
            "agent_name": r["agent_name"],
            "horizon":    r["horizon"],
            "scalar":     scalar,
            "sign":       sign,
            "realized":   realized,
            "pnl":        sign * scalar * realized,
        })
    return out


def _summarize(pnls: list[dict], label: str) -> dict:
    """Sharpe + max-DD + win-rate + total return + N over per-row pnls,
    aggregating to daily curves to get a Sharpe with meaningful units."""
    if not pnls:
        return {"label": label, "n": 0}
    daily: dict[date, float] = defaultdict(float)
    for r in pnls:
        daily[r["trade_date"]] += r["pnl"]
    sorted_days = sorted(daily.keys())
    daily_pnl = [daily[d] for d in sorted_days]

    n = len(pnls)
    total = sum(daily_pnl)
    mean_d = statistics.mean(daily_pnl) if daily_pnl else 0.0
    sd_d = statistics.pstdev(daily_pnl) if len(daily_pnl) > 1 else 0.0
    sharpe = (mean_d / sd_d * math.sqrt(252)) if sd_d > 0 else 0.0

    # Equity + max drawdown
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in daily_pnl:
        eq += p
        peak = max(peak, eq)
        dd = peak - eq
        max_dd = max(max_dd, dd)

    wins = sum(1 for r in pnls if r["pnl"] > 0)
    losses = sum(1 for r in pnls if r["pnl"] < 0)

    return {
        "label":       label,
        "n":           n,
        "n_days":      len(sorted_days),
        "total_pnl":   round(total, 4),
        "mean_daily":  round(mean_d, 4),
        "sd_daily":    round(sd_d, 4),
        "sharpe_ann":  round(sharpe, 3),
        "max_drawdown": round(max_dd, 4),
        "win_rate":    round(wins / max(wins + losses, 1), 3),
    }


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("(no rows)")
        return
    cols = ["label", "n", "n_days", "total_pnl", "mean_daily",
            "sd_daily", "sharpe_ann", "max_drawdown", "win_rate"]
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


async def main() -> int:
    args = _parse_args()
    if not args.functional and not args.all_functionals:
        raise SystemExit("pass --functional NAME or --all-functionals")

    start, end = _resolve_window(args)
    log.info("replay window: %s → %s", start, end)

    rows = await _fetch_rows(start, end, args.agent, args.horizon, args.include_synthetic)
    if not rows:
        log.warning("no qualifying rows found")
        return 2
    log.info("loaded %d distribution rows", len(rows))

    from meta_agent import conviction_functionals
    if args.all_functionals:
        names = conviction_functionals.list_functionals()
    else:
        names = [args.functional]

    summaries: list[dict] = []
    for name in names:
        pnls = _functional_pnls(rows, name)
        summaries.append(_summarize(pnls, label=f"f:{name}"))
        # Also report per-horizon breakdown
        per_h: dict[str, list[dict]] = defaultdict(list)
        for r in pnls:
            per_h[r["horizon"]].append(r)
        for h in sorted(per_h):
            summaries.append(_summarize(per_h[h], label=f"f:{name}/h:{h}"))

    if args.baseline_scalar:
        base = _baseline_pnls(rows)
        summaries.append(_summarize(base, label="baseline:legacy-scalar"))

    print()
    _print_table(summaries)
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    except SystemExit:
        raise
    except Exception:
        log.exception("replay crashed")
        rc = 1
    sys.exit(rc)
