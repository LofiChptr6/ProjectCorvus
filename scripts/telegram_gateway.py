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
import time
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
from approval import proposals as _proposals  # noqa: E402
from concierge import investigate_session as _investigate  # noqa: E402

import re as _re  # noqa: E402

# Fast-path matcher: y / yes / n / no, optionally followed by a short proposal id
# (6+ hex chars). Anything else falls through to Claude Code.
_YN_RE = _re.compile(r"^\s*(y|yes|n|no)(?:\s+([0-9a-fA-F]{6,}))?\s*$", _re.IGNORECASE)

# Slash-command shortcuts so the user doesn't have to copy 8-char hex IDs
# buried in the Telegram stream.
#   /y                → approve oldest pending
#   /n                → reject oldest pending
#   /y 2 / /n 3       → approve/reject the Nth pending (1-indexed in /pending order)
#   /y eb7d0a7d       → approve by short id
#   /pending          → list pending proposals (numbered)
_SLASH_YN_RE = _re.compile(
    r"^\s*/(y|n)(?:\s+(\d+|[0-9a-fA-F]{6,}))?\s*$",
    _re.IGNORECASE,
)
_SLASH_PENDING_RE = _re.compile(r"^\s*/(pending|list)\s*$", _re.IGNORECASE)


def _fmt_pending_list(pending: list[dict]) -> str:
    if not pending:
        return "✅ No pending proposals."
    lines = [f"📋 *Pending proposals ({len(pending)})*", ""]
    for i, p in enumerate(pending, 1):
        lines.append(f"{i}. `{p['id'][:8]}` — {p['title']}")
    lines.append("")
    lines.append("Tap a button on a proposal, or send `/y N` / `/n N` (e.g. `/y 1`).")
    return "\n".join(lines)


async def _try_proposal_fastpath(text: str) -> bool:
    """If the message is a y/n proposal verdict (legacy `y`/`n` form, slash
    form `/y`/`/n`, or `/pending` listing), resolve it in-process and return
    True (so the gateway skips spawning a Claude session). Otherwise return
    False so the message routes to Claude as before."""

    # /pending → list, no resolution
    if _SLASH_PENDING_RE.match(text):
        pending = sorted(_proposals.list_pending(), key=lambda x: x["created_at"])
        try:
            await send_message(_fmt_pending_list(pending))
        except Exception as exc:
            log.warning("/pending send failed: %s", exc)
        return True

    # /y or /n with optional Nth-index or short id
    sm = _SLASH_YN_RE.match(text)
    if sm:
        verdict = sm.group(1).lower()
        arg = sm.group(2)
        approved = verdict == "y"
        reason_str = f"Telegram slash via gateway: {text.strip()}"
        if arg is None:
            prop = _proposals._resolve_oldest(approved, reason=reason_str)
        elif arg.isdigit():
            pending = sorted(_proposals.list_pending(), key=lambda x: x["created_at"])
            idx = int(arg) - 1
            if 0 <= idx < len(pending):
                prop = _proposals._resolve(
                    pending[idx]["id"][:8], approved, reason=reason_str
                )
            else:
                try:
                    await send_message(
                        f"⚠️ No pending proposal #{arg}. "
                        f"Send `/pending` to see the current list."
                    )
                except Exception:
                    pass
                return True
        else:
            prop = _proposals._resolve(arg, approved, reason=reason_str)
        if prop:
            verb = "Approved" if approved else "Rejected"
            icon = "✅" if approved else "❌"
            try:
                await send_message(f"{icon} {verb}: `{prop['id'][:8]}` — {prop['title']}")
            except Exception as exc:
                log.warning("ack send failed: %s", exc)
            log.info("Slash-path resolved proposal %s → %s", prop["id"][:8],
                     "approved" if approved else "rejected")
        else:
            try:
                await send_message("⚠️ No matching pending proposal.")
            except Exception:
                pass
        return True

    # Legacy `y`/`n`/`yes`/`no` (with optional short id) — kept for backwards
    # compatibility with existing muscle memory.
    m = _YN_RE.match(text)
    if not m:
        return False
    verdict = m.group(1).lower()
    short_id = m.group(2)
    approved = verdict in ("y", "yes")
    reason_str = f"Telegram reply via gateway: {text.strip()}"
    if short_id:
        prop = _proposals._resolve(short_id, approved, reason=reason_str)
    else:
        prop = _proposals._resolve_oldest(approved, reason=reason_str)
    if prop:
        verb = "Approved" if approved else "Rejected"
        icon = "✅" if approved else "❌"
        try:
            await send_message(f"{icon} {verb}: `{prop['id'][:8]}` — {prop['title']}")
        except Exception as exc:
            log.warning("ack send failed: %s", exc)
        log.info("Fast-path resolved proposal %s → %s", prop["id"][:8],
                 "approved" if approved else "rejected")
        return True
    # No matching proposal — let it fall through to Claude (so user can ask
    # "what does y mean again" rather than getting silent no-op).
    return False


# ── /strategy-investigate session routing ────────────────────────────────────

_SLASH_INVESTIGATE_RE = _re.compile(
    r"^\s*/strategy[-_]investigate(?:\s+(\w+))?\s*$", _re.IGNORECASE,
)
_SLASH_END_RE = _re.compile(r"^\s*/(end|done|stop)\s*$", _re.IGNORECASE)
_SLASH_STATUS_RE = _re.compile(r"^\s*/(session|investigating)\s*$", _re.IGNORECASE)


# Telegram caps text at 4096 chars. Split long claude replies.
def _chunk_for_telegram(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        # Try to break on a paragraph boundary near the limit.
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks


async def _send_investigate_reply(text: str) -> None:
    for i, chunk in enumerate(_chunk_for_telegram(text)):
        suffix = f"\n\n…({i+1}/?)" if i > 0 or len(_chunk_for_telegram(text)) > 1 else ""
        # Try Markdown first; if claude's reply has unbalanced specials it'll
        # fall back to plain text inside send_message itself.
        try:
            await send_message(chunk + suffix)
        except Exception as exc:
            log.warning("investigate reply send failed: %s", exc)


async def _try_investigate_fastpath(text: str) -> bool:
    """Handle /strategy-investigate <agent>, /end, /session, and free-text
    while a session is active. Returns True if the gateway should NOT spawn
    a Claude terminal for this message."""

    # /strategy-investigate <agent>
    inv_m = _SLASH_INVESTIGATE_RE.match(text)
    if inv_m:
        agent = (inv_m.group(1) or "").lower()
        if not agent:
            try:
                await send_message(
                    "Usage: `/strategy-investigate <agent>`\n"
                    "Agents: " + ", ".join(sorted(_investigate.VALID_AGENTS))
                )
            except Exception:
                pass
            return True
        if agent not in _investigate.VALID_AGENTS:
            try:
                await send_message(
                    f"Unknown agent `{agent}`. Pick one: " +
                    ", ".join(sorted(_investigate.VALID_AGENTS))
                )
            except Exception:
                pass
            return True
        existing = _investigate.current()
        if existing:
            try:
                await send_message(
                    f"⚠️ Already investigating *{existing['agent_name']}* "
                    f"(turn {existing.get('turn_count', 0)}). End it with `/end` first."
                )
            except Exception:
                pass
            return True
        _investigate.start(agent)
        log.info("investigate session start agent=%s", agent)
        # Fire the auto-load briefing as the first turn.
        try:
            await send_message(
                f"🔬 *Investigation session started — {agent}*\n"
                f"Loading state…"
            )
        except Exception:
            pass
        try:
            reply, _ = await _investigate.run_turn(
                "Run STEP 0 and print the briefing. I'll ask follow-ups."
            )
        except Exception as exc:
            log.exception("investigate first turn failed")
            reply = f"⚠️ Failed to start session: {type(exc).__name__}: {exc}"
            _investigate.end()
        await _send_investigate_reply(reply)
        return True

    # /end (only meaningful while a session is active; otherwise let the user
    # see a friendly note)
    if _SLASH_END_RE.match(text):
        ended = _investigate.end()
        if ended:
            try:
                await send_message(
                    f"👋 Investigation session ended — *{ended['agent_name']}* "
                    f"({ended.get('turn_count', 0)} turns). "
                    f"Audit trail: `git diff agents/ .claude/commands/` and the relevant DB tables."
                )
            except Exception:
                pass
        else:
            try:
                await send_message("No active investigation session to end.")
            except Exception:
                pass
        return True

    # /session — quick status
    if _SLASH_STATUS_RE.match(text):
        s = _investigate.current()
        if s:
            mins = int((time.time() - s["started_at"]) / 60)
            try:
                await send_message(
                    f"🔬 Investigating *{s['agent_name']}* — "
                    f"turn {s.get('turn_count', 0)}, {mins} min in. `/end` to close."
                )
            except Exception:
                pass
        else:
            try:
                await send_message("No active investigation session.")
            except Exception:
                pass
        return True

    # Free text while session active → route through claude --resume
    if _investigate.is_active():
        # Don't intercept other slash commands the user might want during a
        # session (/y, /n, /pending) — those are handled before us.
        if text.startswith("/"):
            return False
        try:
            await send_message("…thinking", parse_mode=None)
        except Exception:
            pass
        try:
            reply, _ = await _investigate.run_turn(text)
        except Exception as exc:
            log.exception("investigate turn failed")
            reply = f"⚠️ Turn failed: {type(exc).__name__}: {exc}"
        await _send_investigate_reply(reply)
        return True

    return False


# ── Inline-keyboard callback handler ──────────────────────────────────────────
_CALLBACK_RE = _re.compile(r"^(approve|reject)_([0-9a-fA-F]{6,})$")


async def _handle_callback_query(client: httpx.AsyncClient, cq: dict) -> None:
    """Resolve a callback_query from an inline-keyboard tap. Acks the spinner
    and, when the proposal resolves cleanly, edits the original message to
    show the verdict so the buttons can no longer be re-tapped."""
    cq_id = cq.get("id", "")
    data = cq.get("data", "") or ""
    msg = cq.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id") or "")
    msg_id = msg.get("message_id")

    m = _CALLBACK_RE.match(data)
    base = f"https://api.telegram.org/bot{_token()}"

    if not m:
        try:
            await client.post(f"{base}/answerCallbackQuery",
                              data={"callback_query_id": cq_id, "text": "Unknown action"})
        except Exception:
            pass
        return

    action = m.group(1)
    short_id = m.group(2)
    approved = action == "approve"
    prop = _proposals._resolve(
        short_id, approved,
        reason=f"Telegram inline button: {action}",
    )

    if not prop:
        try:
            await client.post(f"{base}/answerCallbackQuery", data={
                "callback_query_id": cq_id,
                "text": "Already resolved or not found",
                "show_alert": False,
            })
        except Exception:
            pass
        return

    verb = "Approved" if approved else "Rejected"
    icon = "✅" if approved else "❌"
    try:
        await client.post(f"{base}/answerCallbackQuery", data={
            "callback_query_id": cq_id,
            "text": f"{icon} {verb}",
        })
    except Exception:
        pass

    # Strip buttons + append verdict so the user sees a final state.
    if chat_id and msg_id is not None:
        new_text = (
            f"{icon} *{verb}* — `{prop['id'][:8]}`\n\n"
            f"*Title:* {prop['title']}\n\n"
            f"{prop['details']}"
        )
        try:
            await client.post(f"{base}/editMessageText", json={
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": new_text,
                "parse_mode": "Markdown",
                "reply_markup": {"inline_keyboard": []},
            })
        except Exception as exc:
            log.warning("editMessageText after callback failed: %s", exc)

    log.info("Callback resolved proposal %s → %s", prop["id"][:8],
             "approved" if approved else "rejected")


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
    r = await client.get(url, params={
        "timeout": 0,
        "allowed_updates": '["message","callback_query"]',
    })
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
                    "allowed_updates": '["message","callback_query"]',
                })
                r.raise_for_status()
                for update in r.json().get("result", []):
                    offset = update["update_id"] + 1

                    # Inline-button taps (✅ / ❌ on a proposal ping) come in as
                    # callback_query, not message. Handle and continue.
                    cq = update.get("callback_query")
                    if cq:
                        cq_chat_id = str((cq.get("message") or {}).get("chat", {}).get("id") or "")
                        if cq_chat_id and cq_chat_id != chat_id:
                            log.warning("Ignored foreign callback_query chat %s", cq_chat_id)
                            continue
                        try:
                            await _handle_callback_query(client, cq)
                        except Exception as exc:
                            log.warning("callback_query handler failed: %s", exc)
                        continue

                    msg = update.get("message", {})
                    if str(msg.get("chat", {}).get("id")) != chat_id:
                        log.warning("Ignored foreign chat %s",
                                    msg.get("chat", {}).get("id"))
                        continue
                    text = (msg.get("text") or "").strip()
                    if not text:
                        continue
                    # Fast-path: y/n proposal verdict — resolved in-process,
                    # no Claude session spawned. Skip on miss, fall through.
                    try:
                        if await _try_proposal_fastpath(text):
                            continue
                    except Exception as exc:
                        log.warning("fast-path check failed (%s) — falling through to Claude", exc)

                    # Investigation session: /strategy-investigate <agent>,
                    # /end, /session, plus all free-text while a session is
                    # active. When a session is live, free-text never spawns
                    # a terminal — it routes through claude -p --resume.
                    try:
                        if await _try_investigate_fastpath(text):
                            continue
                    except Exception as exc:
                        log.warning("investigate fastpath failed (%s) — falling through to Claude", exc)

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
