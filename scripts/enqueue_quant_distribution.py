#!/usr/bin/env python3
"""Enqueue `quant_distribution_compute` jobs for heavy probabilistic models.

These models (HMM, LightGBM) are too expensive to inline in the hourly
review path, so they run via the agent_job queue. This script is what the
periodic cron / agent's hourly review uses to push a batch of jobs.

Usage:
    # Enqueue HMM forecasts for one symbol
    python scripts/enqueue_quant_distribution.py \\
        --agent atlas --model hmm_regime_mix --symbol SPY

    # Enqueue LightGBM for a list of symbols
    python scripts/enqueue_quant_distribution.py \\
        --agent atlas --model lgbm_bin_classifier --symbols SPY QQQ IWM

    # Enqueue HMM + LightGBM for an agent's full watchlist
    python scripts/enqueue_quant_distribution.py \\
        --agent atlas --model hmm_regime_mix --use-watchlist

Coalesce key: "quant:<agent>:<model>:<symbol>" with a 30-minute window —
spamming this every 5 minutes for the same (agent, model, symbol) merges
into one queued job rather than piling up.

Exit codes:
    0 ok (some or all jobs queued)
    1 fatal (no agent/model, DB unreachable)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
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

log = logging.getLogger("enqueue_quant_distribution")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

# Heavy models run with priority 15 — lower than hourly ticker_review (10) so
# the review path stays responsive, higher than nothing-else-queued slop.
PRIORITY = 15
COALESCE_WINDOW_S = 30 * 60


async def _watchlist_symbols(agent: str) -> list[str]:
    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            # agent_watchlist uses soft-delete (removed_at IS NULL for active rows);
            # see db/schema.py:643 and the idx_agent_watchlist_active partial index.
            "SELECT symbol FROM agent_watchlist WHERE agent_name=$1 AND removed_at IS NULL",
            agent,
        )
    return sorted({(r["symbol"] or "").upper() for r in rows if r["symbol"]})


async def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--agent", required=True)
    p.add_argument("--model", required=True,
                   help="model name under agents/<agent>/models/")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--symbol", help="single ticker")
    g.add_argument("--symbols", nargs="+", help="explicit list of tickers")
    g.add_argument("--use-watchlist", action="store_true",
                   help="enqueue for every active symbol in the agent's watchlist")
    args = p.parse_args()

    from db import store

    if args.symbol:
        symbols = [args.symbol.upper()]
    elif args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        symbols = await _watchlist_symbols(args.agent)
        if not symbols:
            log.error("no active watchlist symbols for agent=%s", args.agent)
            return 1

    enqueued = 0
    coalesced = 0
    for sym in symbols:
        coalesce_key = f"quant:{args.agent}:{args.model}:{sym}"
        try:
            res = await store.enqueue_job_coalesced(
                agent_name=args.agent,
                job_type="quant_distribution_compute",
                payload={"model_name": args.model, "symbol": sym},
                priority=PRIORITY,
                coalesce_key=coalesce_key,
                coalesce_window_s=COALESCE_WINDOW_S,
            )
        except Exception as exc:
            log.warning("enqueue failed for %s/%s/%s: %s: %s",
                        args.agent, args.model, sym, type(exc).__name__, exc)
            continue
        if res.get("action") == "enqueued":
            enqueued += 1
        else:
            coalesced += 1
        log.info("  %s %s/%s/%s job_id=%s",
                 res["action"], args.agent, args.model, sym, res.get("job_id"))

    log.info("enqueue done: enqueued=%d coalesced=%d total=%d",
             enqueued, coalesced, enqueued + coalesced)
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
