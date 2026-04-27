#!/usr/bin/env python3
"""Verify IBKR Gateway is reachable. Run this before anything else."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
import yaml

load_dotenv()


async def main():
    config_path = Path("config.yaml")
    if not config_path.exists():
        print("ERROR: config.yaml not found. Copy config.example.yaml → config.yaml")
        sys.exit(1)

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    print(f"Config loaded. Trading mode: {cfg.get('trading', {}).get('mode', '?')}")

    print("\n[1/2] Connecting to IBKR Gateway...")
    try:
        from ibkr.client import configure, get_ib
        configure(cfg)
        ib = await get_ib()
        print("  ✓ Connected to IBKR")

        print("\n[2/2] Fetching account summary...")
        from ibkr.account import get_account_summary
        summary = await get_account_summary()
        print(f"  ✓ NAV: ${summary.get('nav', 0):,.2f}")
        print(f"  ✓ Cash: ${summary.get('cash', 0):,.2f}")
        print(f"  ✓ Mode: {summary.get('mode', '?')}")

    except Exception as e:
        print(f"  ✗ IBKR connection failed: {e}")
        print("  → Is IB Gateway running? Is API enabled in IB Gateway settings?")
        sys.exit(1)

    print("\n✅ All systems OK. Ready to trade.")


if __name__ == "__main__":
    asyncio.run(main())
