"""One-shot: flatten every position via SELL MKT orders through the IBKR daemon.

Use when the desk needs a clean book before a rebuild (e.g. conviction
pipeline outage left orphan positions un-backed). Orders placed via
`/place_order` on the daemon — bypasses the mcp_server.place_order risk
checks because pure-close orders don't need them, but every fill still
writes to `orders` and `fills` tables via the daemon's fill callback so
the audit trail is intact.

After this runs, the next mike-allocator turn will see an empty book and
rebuild from active convictions.

Usage:
    python scripts/flatten_all.py --dry-run   # print plan, place nothing
    python scripts/flatten_all.py             # place SELL MKT for every long

The script refuses to run if any short positions exist (the desk has no
short stock — would need BUY-to-cover orders we haven't implemented).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import find_dotenv, load_dotenv
    found = find_dotenv(usecwd=True) or str(REPO_ROOT / ".env")
    if Path(found).exists():
        load_dotenv(found)
except Exception:
    pass

DAEMON_URL = "http://127.0.0.1:7790"
TOKEN = os.environ.get("IBKR_DAEMON_TOKEN", "")

REASON = "manual flatten: close all positions; clean book before conviction-driven rebuild"
AGENT_NAME = "mike"  # attribution: the allocator owns flatten operations


async def _get(client: httpx.AsyncClient, path: str) -> dict | list:
    r = await client.get(f"{DAEMON_URL}{path}", headers={"Authorization": f"Bearer {TOKEN}"})
    r.raise_for_status()
    return r.json()


async def _post(client: httpx.AsyncClient, path: str, body: dict) -> dict:
    r = await client.post(
        f"{DAEMON_URL}{path}",
        json=body,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    return {"status_code": r.status_code, "body": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text}


async def _send_telegram(text: str) -> None:
    try:
        from approval.telegram import send_message
        await send_message(
            text, parse_mode="Markdown",
            source_ref={"kind": "system_alert", "alert_kind": "flatten_all",
                        "author_agent": "system",
                        "subject": "manual_flatten"},
        )
    except Exception as e:
        print(f"[warn] telegram failed: {e}", file=sys.stderr)


async def amain(dry_run: bool) -> int:
    if not TOKEN:
        print("ERROR: IBKR_DAEMON_TOKEN not set (.env loaded?)", file=sys.stderr)
        return 2

    async with httpx.AsyncClient(timeout=30.0) as client:
        health = await _get(client, "/healthz")
        if not isinstance(health, dict) or not health.get("connected"):
            print(f"ERROR: daemon not connected: {health}", file=sys.stderr)
            return 2
        mode = health.get("mode", "?")
        positions = await _get(client, "/positions")
        if not isinstance(positions, list):
            print(f"ERROR: unexpected positions payload: {positions}", file=sys.stderr)
            return 2

        if not positions:
            print("nothing to flatten — book is already empty.")
            return 0

        shorts = [p for p in positions if float(p.get("quantity", 0)) < 0]
        if shorts:
            print(f"ABORT: {len(shorts)} short positions present; this script only handles longs.", file=sys.stderr)
            for p in shorts:
                print(f"  short: {p['symbol']} qty={p['quantity']}", file=sys.stderr)
            return 2

        total_mv = sum(float(p.get("market_value", 0) or 0) for p in positions)
        n = len(positions)
        print(f"daemon mode={mode}  positions={n}  total_market_value=${total_mv:,.0f}")
        print(f"plan: SELL MKT for every long position; reason={REASON!r}")

        if dry_run:
            print()
            print(f"{'symbol':<8} {'qty':>8}  {'mkt_value':>10}  {'unreal_pnl':>10}")
            print("-" * 44)
            for p in sorted(positions, key=lambda x: -float(x.get("market_value", 0))):
                print(f"{p['symbol']:<8} {float(p['quantity']):>8.0f}  "
                      f"${float(p['market_value']):>9,.0f}  "
                      f"${float(p['unrealized_pnl']):>+9,.2f}")
            print()
            print("DRY-RUN: no orders placed. Re-run without --dry-run to fire.")
            return 0

        results: list[dict] = []
        t_start = time.monotonic()
        for p in positions:
            sym = p["symbol"]
            qty = float(p["quantity"])
            payload = {
                "symbol": sym, "action": "SELL", "quantity": qty,
                "order_type": "MKT", "limit_price": None, "stop_price": None,
                "agent_name": AGENT_NAME, "session_id": "flatten-all",
                "reasoning": REASON,
            }
            res = await _post(client, "/place_order", payload)
            ok = res["status_code"] == 200 and isinstance(res["body"], dict) and res["body"].get("status") == "submitted"
            note = (res["body"].get("ibkr_order_id") if ok else res["body"])
            results.append({"symbol": sym, "qty": qty, "ok": ok, "result": note})
            print(f"  SELL {sym:<6} {int(qty):>4}  → {'ok ibkr_id=' + str(note) if ok else 'FAIL ' + json.dumps(note)}")

        elapsed = time.monotonic() - t_start
        n_ok = sum(1 for r in results if r["ok"])
        n_fail = len(results) - n_ok
        print()
        print(f"done in {elapsed:.1f}s: {n_ok} placed, {n_fail} failed")
        await _send_telegram(
            f"🧹 *flatten\\_all* — placed {n_ok}/{len(results)} SELL MKT orders "
            f"(${total_mv:,.0f} notional). "
            + ("All clean." if n_fail == 0 else f"⚠ {n_fail} failed; see logs.")
        )
        return 0 if n_fail == 0 else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true", help="print plan, place nothing")
    args = p.parse_args()
    return asyncio.run(amain(args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
