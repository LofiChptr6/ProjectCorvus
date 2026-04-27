"""Telegram → Claude Code gateway (cross-platform).

Long-polls Telegram for messages from TELEGRAM_CHAT_ID and, for each new text
message, opens a new terminal window running:

    claude --dangerously-skip-permissions "<message>"

inside the project working directory.

Platforms:
  - Linux: tries ptyxis, gnome-terminal, konsole, xfce4-terminal,
           kitty, alacritty, wezterm, xterm (in that order)
  - macOS: opens Terminal.app via osascript

No Anthropic API key is used — the spawned `claude` CLI authenticates via its
own OAuth session. The gateway only reads TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
from the environment (or .env in the project root).

Run with:
    python scripts/telegram_gateway.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ── Bootstrap: make project root importable + load .env ───────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass  # python-dotenv optional; env may already be exported by the service

import httpx  # noqa: E402

from approval.telegram import _chat_id, _token, send_message  # noqa: E402

# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_WORKDIR = str(PROJECT_ROOT)
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"
IS_MAC = platform.system() == "Darwin"

# Prefix-based cwd routing. A message starting with "[label] ..." launches
# Claude Code in the matching directory. Unprefixed messages use "default".
PROJECTS: dict[str, str] = {
    "trading": "/home/tianyizhang/opus trading",
    "parrot":  "/home/tianyizhang/AI Projects/ProjectParrot",
    "default": DEFAULT_WORKDIR,
}

_PREFIX_RE = re.compile(r"^\s*\[([a-zA-Z0-9_-]+)\]\s*(.*)$", re.DOTALL)


def _route(text: str) -> tuple[str, str]:
    """Parse optional `[label] msg` prefix → (workdir, prompt)."""
    m = _PREFIX_RE.match(text)
    if m:
        label = m.group(1).lower()
        body = m.group(2).strip()
        if label in PROJECTS and body:
            return PROJECTS[label], body
    return PROJECTS["default"], text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("telegram_gateway")


# ── Terminal launchers ────────────────────────────────────────────────────────

def _bash_payload(prompt: str, workdir: str, label: str) -> str:
    """The bash command that runs inside the new terminal.

    Pipeline:
      1. cd into workdir
      2. start typing-indicator loop in background (Telegram 'typing…' bubble)
      3. claude --print runs non-interactively (bypasses trust/onboarding dialogs)
      4. tee shows output in the terminal AND captures it
      5. notify_telegram.py posts the captured text back to Telegram
      6. kill the typing loop, leave the window open
    """
    notify = str(PROJECT_ROOT / "scripts" / "notify_telegram.py")
    typing = str(PROJECT_ROOT / "scripts" / "typing_indicator.py")
    return (
        f"cd {shlex.quote(workdir)} && "
        f"export TELEGRAM_GATEWAY_SESSION=1 && "
        f"python3 {shlex.quote(typing)} & TYPING_PID=$! ; "
        f"{shlex.quote(CLAUDE_BIN)} --dangerously-skip-permissions --print "
        f"{shlex.quote(prompt)} 2>&1 | "
        f"tee /tmp/claude-gateway-last.txt | "
        f"python3 {shlex.quote(notify)} {shlex.quote(label)}; "
        f"kill $TYPING_PID 2>/dev/null; "
        f"exec bash"
    )


def _linux_terminals(workdir: str) -> list[list[str]]:
    return [
        ["ptyxis", "--new-window", "-d", workdir, "--"],
        ["gnome-terminal", f"--working-directory={workdir}", "--"],
        ["konsole", "--workdir", workdir, "-e"],
        ["xfce4-terminal", f"--working-directory={workdir}", "-e"],
        ["kitty", "-d", workdir],
        ["alacritty", "--working-directory", workdir, "-e"],
        ["wezterm", "start", "--cwd", workdir, "--"],
        ["xterm", "-e"],
    ]


def _launch_linux(prompt: str, workdir: str, label: str = "claude") -> bool:
    payload = _bash_payload(prompt, workdir, label)
    terminals = _linux_terminals(workdir)
    for term_args in terminals:
        if not shutil.which(term_args[0]):
            continue
        cmd = list(term_args) + ["bash", "-c", payload]
        try:
            subprocess.Popen(cmd)
            log.info("Launched via %s (cwd=%s)", term_args[0], workdir)
            return True
        except Exception as exc:
            log.warning("%s failed: %s", term_args[0], exc)
    log.error("No working terminal emulator found. Tried: %s",
              [t[0] for t in terminals])
    return False


def _launch_mac(prompt: str, workdir: str, label: str = "claude") -> bool:
    # Write a temp shell script and have Terminal.app run it. Avoids AppleScript
    # quoting hell for arbitrary prompt text.
    notify = str(PROJECT_ROOT / "scripts" / "notify_telegram.py")
    fd, path = tempfile.mkstemp(prefix="claude-gw-", suffix=".sh", text=True)
    with os.fdopen(fd, "w") as f:
        f.write("#!/bin/bash\n")
        f.write(f"cd {shlex.quote(workdir)}\n")
        f.write("export TELEGRAM_GATEWAY_SESSION=1\n")
        f.write(
            f"{shlex.quote(CLAUDE_BIN)} --dangerously-skip-permissions --print "
            f"{shlex.quote(prompt)} 2>&1 | "
            f"tee /tmp/claude-gateway-last.txt | "
            f"python3 {shlex.quote(notify)} {shlex.quote(label)}\n"
        )
        f.write("exec $SHELL\n")
    os.chmod(path, 0o755)
    apple = (
        f'tell application "Terminal" to activate\n'
        f'tell application "Terminal" to do script "{path}"'
    )
    try:
        subprocess.Popen(["osascript", "-e", apple])
        log.info("Launched Terminal.app via osascript (%s, cwd=%s)", path, workdir)
        return True
    except Exception as exc:
        log.error("osascript launch failed: %s", exc)
        return False


def launch_claude(prompt: str, workdir: str, label: str = "claude") -> bool:
    if IS_MAC:
        return _launch_mac(prompt, workdir, label)
    return _launch_linux(prompt, workdir, label)


# ── Telegram polling ──────────────────────────────────────────────────────────

async def _skip_backlog(client: httpx.AsyncClient, url: str) -> int:
    r = await client.get(url, params={"timeout": 0})
    r.raise_for_status()
    offset = 0
    for u in r.json().get("result", []):
        offset = max(offset, u["update_id"] + 1)
    return offset


async def main() -> None:
    chat_id = await _chat_id()
    url = f"https://api.telegram.org/bot{_token()}/getUpdates"
    log.info("Gateway starting (chat_id=%s, default_workdir=%s, platform=%s)",
             chat_id, DEFAULT_WORKDIR, platform.system())
    log.info("Routing labels: %s", ", ".join(sorted(PROJECTS)))

    async with httpx.AsyncClient(timeout=35) as client:
        offset = await _skip_backlog(client, url)
        log.info("Backlog skipped — listening (offset=%d)", offset)

        while True:
            try:
                r = await client.get(url, params={
                    "offset": offset,
                    "timeout": 30,
                    "allowed_updates": '["message"]',
                })
                r.raise_for_status()
                for update in r.json().get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    if str(msg.get("chat", {}).get("id")) != chat_id:
                        log.warning("Ignored foreign chat %s",
                                    msg.get("chat", {}).get("id"))
                        continue
                    text = (msg.get("text") or "").strip()
                    if not text:
                        continue
                    workdir, prompt = _route(text)
                    label = Path(workdir).name
                    log.info("→ [%s] %s", label, prompt[:120])
                    if launch_claude(prompt, workdir, label):
                        # Immediate "typing…" so the user sees acknowledgement
                        # without echoing their prompt back at them. The spawned
                        # bash keeps the indicator alive via typing_indicator.py.
                        try:
                            await client.post(
                                f"https://api.telegram.org/bot{_token()}/sendChatAction",
                                data={"chat_id": chat_id, "action": "typing"},
                            )
                        except Exception as exc:
                            log.warning("typing action failed: %s", exc)
                    else:
                        try:
                            await send_message(
                                "❌ Failed to launch terminal — check gateway logs."
                            )
                        except Exception:
                            pass
            except httpx.HTTPError as exc:
                log.warning("HTTP error: %s — retrying in 3s", exc)
                await asyncio.sleep(3)
            except Exception as exc:
                log.exception("Unexpected error: %s", exc)
                await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
