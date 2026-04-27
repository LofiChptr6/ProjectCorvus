"""Read stdin, post it to Telegram via the bot configured in `.env`.

Used by the gateway's spawned bash to deliver `claude --print` output back
to Telegram without relying on Claude Code's Stop hook (which doesn't fire
in `--print` mode).

Usage:
    claude --print "..." 2>&1 | python3 notify_telegram.py <label>
"""

from __future__ import annotations

import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

MAX_LEN = 3500


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "claude"
    text = sys.stdin.read().strip()
    if not text:
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.stderr.write("[notify_telegram] missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID\n")
        return 0

    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text[:MAX_LEN],
        "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=data,
            ),
            timeout=10,
        ) as r:
            if r.status >= 300:
                sys.stderr.write(f"[notify_telegram] HTTP {r.status}\n")
    except Exception as e:
        sys.stderr.write(f"[notify_telegram] send failed: {e}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
