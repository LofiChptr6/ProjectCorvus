#!/usr/bin/env python3
"""Price-anchored thesis resolver — verifies open theses against bars
instead of trusting the owning agent's self-grade.

The 2026-05-12 audit found agent-driven update_thesis_status produced a
352:69 confirmed:wrong ratio with resolution notes that were mostly
tautologies ("QQQ healthy uptrend → confirmed: QQQ remains in healthy
technical uptrend"). Real verification needs a price reference.

This resolver runs nightly. For each thesis where:
    status = 'open'
    verify_by <= today
    primary_symbol, direction, entry_price are all set
it pulls the current quote (last close after RTH), computes pct change
vs entry_price, and classifies:

    direction='long'  and pct >= +RESOLVE_THRESHOLD_PCT  → confirmed
    direction='long'  and pct <= -RESOLVE_THRESHOLD_PCT  → wrong
    direction='short' and pct <= -RESOLVE_THRESHOLD_PCT  → confirmed
    direction='short' and pct >= +RESOLVE_THRESHOLD_PCT  → wrong
    |pct| < RESOLVE_THRESHOLD_PCT                        → leave open

Resolutions are tagged resolution_source='price_anchored' so the
dashboard can split this cohort from the self_graded backlog.

Exit codes:
    0  ran successfully (any number of resolutions, including zero)
    1  unexpected runtime error
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
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


# Symmetric confirm/wrong threshold. 2% is small enough to catch real moves
# inside a typical thesis window, large enough to ignore intraday noise.
RESOLVE_THRESHOLD_PCT = 2.0


def _setup_logging() -> logging.Logger:
    log_path = _REPO_ROOT / "logs" / "thesis-resolver.log"
    log_path.parent.mkdir(exist_ok=True)
    logger = logging.getLogger("thesis_resolver")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(log_path)
        fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s",
                                          datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(fh)
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(sh)
    return logger


log = _setup_logging()


def _classify(direction: str, pct: float) -> str | None:
    """Return 'confirmed' / 'wrong' / None (ambiguous, leave open)."""
    if direction == "long":
        if pct >= RESOLVE_THRESHOLD_PCT:
            return "confirmed"
        if pct <= -RESOLVE_THRESHOLD_PCT:
            return "wrong"
    elif direction == "short":
        if pct <= -RESOLVE_THRESHOLD_PCT:
            return "confirmed"
        if pct >= RESOLVE_THRESHOLD_PCT:
            return "wrong"
    return None


async def _current_price(symbol: str) -> float | None:
    from data.massive_client import get_quote
    try:
        q = await get_quote(symbol)
    except Exception as exc:
        log.warning("get_quote failed for %s: %s", symbol, exc)
        return None
    for k in ("last", "price", "close"):
        v = q.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    log.warning("get_quote returned no usable price for %s: keys=%s", symbol, list(q.keys()))
    return None


async def main() -> int:
    start = time.time()
    log.info("resolver starting (threshold=±%.2f%%)", RESOLVE_THRESHOLD_PCT)

    from db.schema import get_pool
    from db import store

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, agent_name, title, primary_symbol, direction,
                   entry_price, verify_by, created_at
            FROM agent_thesis
            WHERE status = 'open'
              AND verify_by <= CURRENT_DATE
              AND primary_symbol IS NOT NULL
              AND direction IS NOT NULL
              AND entry_price IS NOT NULL
            ORDER BY verify_by, id
            LIMIT 500
            """,
        )

    if not rows:
        log.info("nothing to resolve (no open theses past verify_by with price anchor)")
        return 0

    log.info("queue: %d theses to evaluate", len(rows))

    # Bucket by symbol so we make one get_quote per distinct ticker.
    by_symbol: dict[str, list[dict]] = {}
    for r in rows:
        by_symbol.setdefault(r["primary_symbol"], []).append(dict(r))

    counts = {"confirmed": 0, "wrong": 0, "ambiguous": 0, "no_quote": 0}
    for symbol, ts in sorted(by_symbol.items()):
        current = await _current_price(symbol)
        if current is None:
            counts["no_quote"] += len(ts)
            log.warning("symbol=%s: skipped %d theses (no quote)", symbol, len(ts))
            continue
        for t in ts:
            entry = float(t["entry_price"])
            if entry <= 0:
                counts["no_quote"] += 1
                continue
            pct = (current - entry) / entry * 100.0
            verdict = _classify(t["direction"], pct)
            if verdict is None:
                counts["ambiguous"] += 1
                log.info(
                    "id=%d sym=%s agent=%s direction=%s entry=%.4f now=%.4f pct=%+.2f%% — AMBIGUOUS (left open)",
                    t["id"], symbol, t["agent_name"], t["direction"], entry, current, pct,
                )
                continue
            note = (
                f"{t['direction']} thesis on {symbol}: entry ${entry:.4f} → close ${current:.4f} "
                f"= {pct:+.2f}% over {(t['verify_by'] - t['created_at'].date()).days}d → {verdict} "
                f"(threshold ±{RESOLVE_THRESHOLD_PCT:.1f}%)"
            )
            await store.update_thesis_status(
                thesis_id=t["id"],
                status=verdict,
                resolution_note=note,
                resolution_source="price_anchored",
            )
            counts[verdict] += 1
            log.info(
                "id=%d sym=%s agent=%s pct=%+.2f%% → %s",
                t["id"], symbol, t["agent_name"], pct, verdict,
            )

    log.info(
        "resolver done in %.1fs — confirmed=%d wrong=%d ambiguous=%d no_quote=%d",
        time.time() - start, counts["confirmed"], counts["wrong"],
        counts["ambiguous"], counts["no_quote"],
    )
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    except Exception:
        log.exception("resolver crashed")
        rc = 1
    sys.exit(rc)
