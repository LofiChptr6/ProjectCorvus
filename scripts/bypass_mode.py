"""Toggle the time-bounded approval bypass.

    python -m scripts.bypass_mode on --hours 3 [--reason "morning rush"]
    python -m scripts.bypass_mode off
    python -m scripts.bypass_mode status

When active, every approval gate (large-order Telegram approval and
strategic proposals) auto-approves until expiry. State persists to
`data/auto_approve_until.json`. See `approval/bypass.py` for consumers.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_on = sub.add_parser("on", help="Enable bypass for N hours")
    p_on.add_argument("--hours", type=float, default=3.0)
    p_on.add_argument("--reason", default="")

    sub.add_parser("off", help="Disable bypass")
    sub.add_parser("status", help="Show bypass status")

    args = parser.parse_args()

    from approval import bypass

    if args.cmd == "on":
        s = bypass.enable(hours=args.hours, reason_note=args.reason)
        print("✅ Bypass enabled")
        print(json.dumps(s, indent=2, default=str))
    elif args.cmd == "off":
        bypass.disable()
        print("✅ Bypass disabled")
    elif args.cmd == "status":
        print(json.dumps(bypass.status(), indent=2, default=str))


if __name__ == "__main__":
    main()
