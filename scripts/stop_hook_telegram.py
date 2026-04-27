"""Claude Code Stop hook → Telegram.

Reads the Stop hook event from stdin, locates the session transcript, extracts
the final assistant message, and posts it to the configured Telegram chat.

Configured per-project via `.claude/settings.json`:

    {
      "hooks": {
        "Stop": [
          {
            "hooks": [
              {"type": "command",
               "command": "python /abs/path/scripts/stop_hook_telegram.py"}
            ]
          }
        ]
      }
    }

Reads TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID from the project `.env` (loaded via
python-dotenv if available) or the environment.

Silent on success — prints a one-line warning to stderr on failure but always
exits 0 so a misconfigured hook never blocks Claude Code.
"""

from __future__ import annotations

import json
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

MAX_LEN = 3500  # Telegram cap is 4096; leave headroom for prefix/markdown


def _warn(msg: str) -> None:
    sys.stderr.write(f"[stop_hook_telegram] {msg}\n")


def _last_assistant_text(transcript_path: str) -> str | None:
    """Walk the JSONL transcript backwards; return the last assistant text."""
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8").splitlines()
    except OSError as e:
        _warn(f"transcript read failed: {e}")
        return None

    for raw in reversed(lines):
        if not raw.strip():
            continue
        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if evt.get("type") != "assistant":
            continue
        msg = evt.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            chunks = [
                blk.get("text", "")
                for blk in content
                if isinstance(blk, dict) and blk.get("type") == "text"
            ]
            text = "".join(chunks).strip()
            if text:
                return text
    return None


def _send(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text[:MAX_LEN],
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.status >= 300:
            _warn(f"telegram HTTP {r.status}")


def main() -> None:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        _warn(f"bad stdin JSON: {e}")
        return

    if event.get("stop_hook_active"):
        return  # avoid loops if a continuation re-triggers the hook

    # Only fire for sessions launched by telegram_gateway.py.
    if os.environ.get("TELEGRAM_GATEWAY_SESSION") != "1":
        return

    transcript = event.get("transcript_path")
    if not transcript:
        _warn("no transcript_path in event")
        return

    text = _last_assistant_text(transcript)
    if not text:
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        _warn("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        return

    cwd = event.get("cwd") or os.getcwd()
    label = Path(cwd).name
    prefix = f"💬 [{label}]\n\n"
    try:
        _send(token, chat_id, prefix + text)
    except Exception as e:
        _warn(f"send failed: {e}")


if __name__ == "__main__":
    try:
        main()
    finally:
        # Always exit 0 — never block Claude Code on hook failures.
        sys.exit(0)
