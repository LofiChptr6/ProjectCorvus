#!/usr/bin/env python3
"""Data-driven leaderboard: which conviction functional has produced the best
P&L for each agent over the recent calibration window?

For every (agent, functional) pair, replays the agent's resolved distributions
in the window using `scripts.replay_conviction_functional`'s pure-Python
helpers, ranks by annualized Sharpe, and prints a leaderboard. With
`--write-yaml`, also updates each agent's YAML with the winning functional —
the next model-backed conviction submission will pick it up automatically via
`meta_agent.conviction_functionals.functional_for_agent`.

Defaults to last 30 days; falls back to whatever resolved-distribution rows
exist if fewer days available.

Usage:
    # Print the leaderboard
    python scripts/suggest_functional_per_agent.py

    # Restrict to last 14 days, only show atlas
    python scripts/suggest_functional_per_agent.py --since-days 14 --agent atlas

    # Apply the winners to YAML (one agent at a time encouraged for safety)
    python scripts/suggest_functional_per_agent.py --agent atlas --write-yaml

Exit codes:
    0 ran successfully (any leaderboard size, including empty)
    1 fatal (DB unreachable, YAML write failed)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
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

log = logging.getLogger("suggest_functional")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

# Minimum rows per (agent, functional) below which we don't trust the ranking.
MIN_SAMPLES = 10


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--since-days", type=int, default=30)
    p.add_argument("--agent", help="restrict to one agent")
    p.add_argument("--write-yaml", action="store_true",
                   help="apply winners to agents/<agent>.yaml")
    return p.parse_args()


async def _fetch_resolved(start: date, agent: str | None) -> list[dict]:
    """Pull rows the replay harness's helpers can chew on."""
    from scripts.replay_conviction_functional import _fetch_rows
    end = date.today()
    return await _fetch_rows(start, end, agent=agent,
                              horizon=None, include_synthetic=False)


def _rank(rows: list[dict], agent: str) -> list[dict]:
    """For one agent's rows: compute the per-functional summary and rank by
    Sharpe (descending), then by total_pnl as the tiebreaker. Returns the
    list of summary dicts ready to print."""
    from scripts.replay_conviction_functional import _functional_pnls, _summarize
    from meta_agent.conviction_functionals import list_functionals
    out: list[dict] = []
    for fname in list_functionals():
        pnls = _functional_pnls(rows, fname)
        summary = _summarize(pnls, label=fname)
        summary["agent"] = agent
        summary["functional"] = fname
        out.append(summary)
    # Best first: Sharpe descending, then total_pnl descending.
    out.sort(key=lambda r: (-(r.get("sharpe_ann") or 0.0),
                            -(r.get("total_pnl") or 0.0)))
    return out


def _print_leaderboard(per_agent: dict[str, list[dict]]) -> None:
    cols = ["agent", "functional", "n", "n_days", "total_pnl",
            "sharpe_ann", "max_drawdown", "win_rate"]
    flat: list[dict] = []
    for agent, ranks in per_agent.items():
        for i, r in enumerate(ranks):
            entry = {c: r.get(c, "") for c in cols}
            entry["agent"] = agent if i == 0 else f"  {agent}"  # indent followers
            flat.append(entry)
    if not flat:
        print("(no data)")
        return
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in flat)) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("-" * len(header))
    for r in flat:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def _winners(per_agent: dict[str, list[dict]]) -> dict[str, str]:
    """Return {agent: best_functional} after filtering for sample size."""
    out: dict[str, str] = {}
    for agent, ranks in per_agent.items():
        for r in ranks:
            if (r.get("n") or 0) >= MIN_SAMPLES and (r.get("sharpe_ann") or 0.0) > 0:
                out[agent] = r["functional"]
                break
    return out


def _apply_winners_to_yaml(winners: dict[str, str]) -> int:
    """Write `conviction_functional: <name>` to each agent's YAML in-place.
    Preserves existing content via roundtrip yaml (PyYAML's default loader/dumper
    is good enough — these files are hand-written, no comments need preserving).

    Returns count of files updated."""
    import yaml
    updated = 0
    for agent, fname in winners.items():
        yaml_path = _REPO_ROOT / "agents" / f"{agent}.yaml"
        if not yaml_path.exists():
            log.warning("skip %s: no YAML at %s", agent, yaml_path)
            continue
        with yaml_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        prior = data.get("conviction_functional")
        if prior == fname:
            log.info("%s: already %s; skipping", agent, fname)
            continue
        data["conviction_functional"] = fname
        with yaml_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)
        log.info("%s: %s → %s", agent, prior or "(unset)", fname)
        updated += 1
    return updated


async def main() -> int:
    args = _parse_args()
    start = date.today() - timedelta(days=args.since_days)
    log.info("leaderboard window: %s → today  (--since-days %d)", start, args.since_days)

    rows = await _fetch_resolved(start, args.agent)
    if not rows:
        log.warning("no resolved distributions in window — nothing to rank")
        return 0
    log.info("loaded %d resolved distribution rows", len(rows))

    by_agent: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_agent[r["agent_name"]].append(r)

    per_agent_ranks: dict[str, list[dict]] = {
        agent: _rank(agent_rows, agent) for agent, agent_rows in by_agent.items()
    }

    print()
    _print_leaderboard(per_agent_ranks)
    print()

    winners = _winners(per_agent_ranks)
    if not winners:
        log.info("no agent has enough samples (>=%d) with positive Sharpe; "
                 "no recommendations written.", MIN_SAMPLES)
        return 0

    print("Recommended functionals (per agent):")
    for agent, fname in winners.items():
        print(f"  {agent}: {fname}")
    print()

    if args.write_yaml:
        try:
            n = _apply_winners_to_yaml(winners)
            log.info("wrote conviction_functional to %d agent YAML(s)", n)
        except Exception as exc:
            log.exception("YAML write failed: %s", exc)
            return 1
    else:
        print("(pass --write-yaml to apply these to agents/<agent>.yaml)")
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    except SystemExit:
        raise
    except Exception:
        log.exception("crashed")
        rc = 1
    sys.exit(rc)
