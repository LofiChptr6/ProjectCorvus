"""Keep Telegram's 'typing…' indicator alive while a long task runs.

Telegram's sendChatAction expires after ~5 s, so we re-send every 4 s.
Started in the background by the gateway's spawned bash; killed when
claude exits.

Usage:
    python3 typing_indicator.py &
    TYPING_PID=$!
    ...long task...
    kill $TYPING_PID 2>/dev/null
"""

from __future__ import annotations

import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return 0

    url = f"https://api.telegram.org/bot{token}/sendChatAction"
    data = urllib.parse.urlencode({"chat_id": chat_id, "action": "typing"}).encode()

    while True:
        try:
            urllib.request.urlopen(
                urllib.request.Request(url, data=data),
                timeout=5,
            ).read()
        except Exception:
            pass  # transient — keep trying
        time.sleep(4)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
