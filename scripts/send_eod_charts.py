"""End-of-day Telegram broadcast: render each sector agent's 7-day P&L curve
(via reporting/pnl_curve.py) and send to the desk's Telegram chat with a
numbers caption.

Triggered by cron after market close every trading day. Sequential — sends
~11 photos in order; small delay between sends to be polite to Telegram's
per-chat rate limit.

Manual run:
    .venv/bin/python -m scripts.send_eod_charts
    .venv/bin/python -m scripts.send_eod_charts --agent rex     # one only
    .venv/bin/python -m scripts.send_eod_charts --no-desk       # skip desk
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env", override=False)
except ImportError:
    pass

log = logging.getLogger("send_eod_charts")

SECTORS = (
    "atlas", "fab", "fabless", "iron", "maya",
    "rex", "titan", "trump", "vera", "volt",
)


async def _caption_for_agent(agent_name: str) -> str:
    from db import store
    states = await store.get_latest_agent_state(agent_name=agent_name)
    if not states:
        return f"{agent_name} — 7d (no agent_state yet — agent hasn't traded)"
    s = states[0]
    return (
        f"{agent_name} — last 7 trading days\n"
        f"realized ${float(s['realized_pnl']):+,.2f}  "
        f"unrealized ${float(s['unrealized_pnl']):+,.2f}\n"
        f"total ${float(s['total_pnl']):+,.2f}  "
        f"({int(s['n_positions'])} open positions)"
    )


async def _send_one_agent(agent_name: str, sleep_after: float = 1.5) -> bool:
    from reporting.pnl_curve import render_agent_curve
    from approval.telegram import send_photo
    try:
        path = await render_agent_curve(agent_name, since="7d")
        caption = await _caption_for_agent(agent_name)
        result = await send_photo(str(path), caption)
        ok = result is not None
        log.info("%s: %s (%s)", agent_name, "sent" if ok else "FAILED", path)
    except Exception as exc:
        log.exception("%s: render/send blew up: %s", agent_name, exc)
        ok = False
    if sleep_after > 0:
        await asyncio.sleep(sleep_after)
    return ok


async def _send_desk(sleep_after: float = 1.5) -> bool:
    from reporting.pnl_curve import render_desk_curve
    from approval.telegram import send_photo
    try:
        path = await render_desk_curve(since="7d")
        caption = (
            "DESK — last 7 trading days\n"
            "Sum across all agents per hour."
        )
        result = await send_photo(str(path), caption)
        ok = result is not None
        log.info("desk: %s (%s)", "sent" if ok else "FAILED", path)
    except Exception as exc:
        log.exception("desk: render/send blew up: %s", exc)
        ok = False
    if sleep_after > 0:
        await asyncio.sleep(sleep_after)
    return ok


async def main(only: str | None, include_desk: bool) -> int:
    sent = 0
    failed = 0

    if only:
        ok = await _send_one_agent(only)
        return 0 if ok else 1

    if include_desk:
        if await _send_desk():
            sent += 1
        else:
            failed += 1

    for agent in SECTORS:
        if await _send_one_agent(agent):
            sent += 1
        else:
            failed += 1

    log.info("EOD broadcast complete: %d sent, %d failed", sent, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--agent", help="render+send only this agent (skip rest)")
    p.add_argument("--no-desk", action="store_true",
                   help="skip the desk-aggregated chart")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sys.exit(asyncio.run(main(only=args.agent, include_desk=not args.no_desk)))
