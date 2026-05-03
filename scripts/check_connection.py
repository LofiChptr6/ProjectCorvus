#!/usr/bin/env python3
"""Verify the ibkr-daemon (and through it, IB Gateway) is reachable.

The daemon at 127.0.0.1:7790 owns the live IBKR connection. This preflight
script asks `/healthz` (unauthenticated) and `/balances` (authenticated) so
both transport and credentials are validated.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()


async def main():
    from ibkr import _rpc
    from ibkr.account import get_account_summary

    print("[1/2] Pinging ibkr-daemon /healthz...")
    try:
        health = await _rpc.get("/healthz")
    except Exception as e:
        print(f"  ✗ daemon unreachable: {e}")
        print("  → systemctl --user status ibkr-daemon")
        sys.exit(1)

    if not health.get("connected"):
        print(f"  ✗ daemon up but IBKR disconnected (mode={health.get('mode')})")
        print("  → check IB Gateway is running on configured host:port")
        sys.exit(1)
    print(f"  ✓ daemon up — mode={health['mode']}, uptime={health['uptime_s']}s, "
          f"clientId={health['client_id']}")

    print("\n[2/2] Fetching account summary via daemon...")
    try:
        summary = await get_account_summary()
        print(f"  ✓ NAV: ${summary.get('nav', 0):,.2f}")
        print(f"  ✓ Cash: ${summary.get('cash', 0):,.2f}")
        print(f"  ✓ Mode: {summary.get('mode', '?')}")
    except Exception as e:
        print(f"  ✗ account summary failed: {e}")
        sys.exit(1)
    finally:
        await _rpc.close()

    print("\n✅ All systems OK. Ready to trade.")


if __name__ == "__main__":
    asyncio.run(main())
