"""Telegram-driven `/strategy-investigate` sessions.

The Telegram gateway calls into this module to start, end, and route
messages through an investigation session. Each turn spawns

    claude -p --output-format json --resume <sid>

headless (no terminal) and captures the resulting `session_id` for
continuity on the next turn. The first turn invokes the
`/strategy-investigate <agent>` skill which loads the full agent context.
Subsequent turns inherit that context via `--resume`.

State at `data/investigate_session.json` survives gateway restarts.

Limits:
- One active session at a time (single-user desk).
- 60-minute idle timeout — the next message after a stale session
  ends it cleanly and tells the user.
- Per-turn 600s subprocess cap (a long tool-use turn can take a
  couple of minutes; we don't want a hung claude to block the
  gateway forever).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SESSION_FILE = PROJECT_ROOT / "data" / "investigate_session.json"
IDLE_TIMEOUT_S = 60 * 60
INNER_TIMEOUT_S = 600

VALID_AGENTS = {
    "atlas", "fab", "fabless", "iron", "maya", "rex", "titan",
    "trump", "vera", "volt", "mike", "cassidy",
}

log = logging.getLogger(__name__)


def _load() -> Optional[dict]:
    if not SESSION_FILE.exists():
        return None
    try:
        return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("session load failed: %s", exc)
        return None


def _save(state: dict) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _clear() -> None:
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()


def is_active() -> bool:
    """True if a non-stale session exists. Stale sessions are auto-cleared."""
    s = _load()
    if not s:
        return False
    age = time.time() - s.get("last_msg_at", 0)
    if age > IDLE_TIMEOUT_S:
        log.info("auto-clearing stale session for agent=%s (idle %ds)",
                 s.get("agent_name"), int(age))
        _clear()
        return False
    return True


def current() -> Optional[dict]:
    """Return the active session state dict, or None."""
    return _load() if is_active() else None


def start(agent_name: str) -> dict:
    state = {
        "agent_name": agent_name,
        "started_at": time.time(),
        "last_msg_at": time.time(),
        "claude_session_id": None,
        "turn_count": 0,
    }
    _save(state)
    return state


def end() -> Optional[dict]:
    """End the active session. Returns the cleared state dict (or None)."""
    s = _load()
    _clear()
    return s


async def run_turn(user_text: str) -> tuple[str, bool]:
    """Run one investigation turn. Returns (assistant_text, ok)."""
    s = _load()
    if not s:
        return ("⚠️ No active investigation session. Start one with `/strategy-investigate <agent>`.", False)

    age = time.time() - s.get("last_msg_at", 0)
    if age > IDLE_TIMEOUT_S:
        _clear()
        return (f"⏱ Session for *{s['agent_name']}* timed out (idle {int(age/60)} min). "
                f"Start fresh with `/strategy-investigate {s['agent_name']}`.", False)

    s["last_msg_at"] = time.time()
    s["turn_count"] = s.get("turn_count", 0) + 1

    model = os.environ.get("INVESTIGATE_MODEL", "claude-sonnet-4-6")

    if s.get("claude_session_id") is None:
        prompt = (
            f"/strategy-investigate {s['agent_name']}\n\n"
            f"The user's first Telegram message in this session is:\n\n{user_text}\n\n"
            f"Run STEP 0 (auto-load) and respond. Keep responses tight — "
            f"this is rendering on Telegram, target ≤25 lines unless the user "
            f"asks for more."
        )
        cmd = [
            "claude", "-p", "--output-format", "json",
            "--dangerously-skip-permissions",
            "--model", model,
            prompt,
        ]
    else:
        cmd = [
            "claude", "-p", "--output-format", "json",
            "--dangerously-skip-permissions",
            "--resume", s["claude_session_id"],
            user_text,
        ]

    log.info("investigate turn=%d agent=%s resume=%s",
             s["turn_count"], s["agent_name"], bool(s.get("claude_session_id")))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=INNER_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        _save(s)  # preserve last_msg_at bump even on timeout
        return (
            f"⏱ Turn timed out after {INNER_TIMEOUT_S}s. Session still active — "
            f"try a smaller question, or `/end` to close the session.",
            False,
        )

    if proc.returncode != 0:
        err = stderr_b.decode("utf-8", errors="replace")[:600]
        log.warning("claude exit=%d turn=%d agent=%s\n%s",
                    proc.returncode, s["turn_count"], s["agent_name"], err)
        _save(s)
        return (f"⚠️ Claude exited with code {proc.returncode}.\n```\n{err}\n```", False)

    try:
        out = json.loads(stdout_b.decode("utf-8", errors="replace"))
    except Exception as exc:
        _save(s)
        return (f"⚠️ Could not parse claude output: {exc}", False)

    assistant_text = out.get("result") or "(no result)"
    new_sid = out.get("session_id")
    if new_sid:
        s["claude_session_id"] = new_sid
    _save(s)

    return (assistant_text, True)
