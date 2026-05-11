"""Telegram bot integration: send messages and poll for y/n replies.

Chat ID resolution (in order):
  1. TELEGRAM_CHAT_ID env var (explicit override)
  2. data/telegram_chat_id.txt cache file
  3. getUpdates — take the first user who messaged the bot, cache it

To onboard: set TELEGRAM_BOT_TOKEN, start the bot in Telegram, send any message,
then any call to send_message() auto-discovers and caches the chat_id.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_BASE = "https://api.telegram.org/bot{token}"
_CHAT_ID_CACHE = Path("data/telegram_chat_id.txt")

# Markdown-mode special chars per Telegram bot API. Escape these in any string
# that is interpolated from user/agent input before passing to send_message().
_MD_SPECIALS = "_*[]()~`>#+-=|{}.!"


def escape_markdown(text: str) -> str:
    """Escape Telegram Markdown specials. Use on any user/agent-controlled value
    that's interpolated into a parse_mode='Markdown' message — otherwise a stray
    backtick or bracket can break formatting or smuggle a phishing link."""
    if not text:
        return ""
    out = []
    for ch in text:
        if ch in _MD_SPECIALS:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _token() -> str:
    t = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not t:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment")
    return t


async def _discover_chat_id() -> Optional[str]:
    """Call getUpdates and return the chat_id of the first message sender."""
    url = _BASE.format(token=_token()) + "/getUpdates"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(url, params={"limit": 10})
            r.raise_for_status()
            data = r.json()
            for update in data.get("result", []):
                msg = update.get("message") or update.get("edited_message")
                if msg and "chat" in msg:
                    return str(msg["chat"]["id"])
        except Exception as exc:
            log.error("Telegram chat_id discovery failed: %s", exc)
    return None


async def _chat_id() -> str:
    env_val = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if env_val:
        return env_val

    if _CHAT_ID_CACHE.exists():
        cached = _CHAT_ID_CACHE.read_text(encoding="utf-8").strip()
        if cached:
            return cached

    discovered = await _discover_chat_id()
    if not discovered:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID not set and no pending messages to bot. "
            "Send any message to the bot in Telegram, then retry."
        )

    _CHAT_ID_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _CHAT_ID_CACHE.write_text(discovered, encoding="utf-8")
    log.info("Auto-discovered and cached Telegram chat_id=%s", discovered)
    return discovered


async def _log_outbound_safe(
    chat_id: Optional[str],
    kind: str,
    content: str,
    *,
    role: Optional[str] = None,
    tool_calls: Optional[list] = None,
    tool_call_id: Optional[str] = None,
    meta: Optional[dict] = None,
) -> None:
    """Best-effort write to telegram_message — never break the send path."""
    try:
        from db.store import log_outbound
        await log_outbound(
            chat_id, kind, content,
            role=role, tool_calls=tool_calls, tool_call_id=tool_call_id, meta=meta,
        )
    except Exception as exc:
        log.debug("telegram_message log_outbound skipped: %s", exc)


async def send_message(
    text: str,
    parse_mode: Optional[str] = "Markdown",
    reply_markup: Optional[dict] = None,
    *,
    kind: str = "push",
    role: Optional[str] = None,
    tool_calls: Optional[list] = None,
    meta: Optional[dict] = None,
) -> Optional[dict]:
    """Send a message to the configured (or auto-detected) Telegram chat.

    parse_mode=None means plain text. Omit the field entirely in that case —
    Telegram returns 400 "unsupported parse_mode" for a literal null.

    reply_markup, if given, is the Telegram inline-keyboard / reply-keyboard
    markup dict (e.g. {"inline_keyboard": [[{"text": "✅", "callback_data": "approve_xxx"}]]}).

    `kind` tags the outbound row in the telegram_message log. Defaults to 'push'
    (agent-initiated desk notifications). Pass 'approval' for proposal pings
    and resolution confirmations, 'concierge_reply' for LLM replies, or
    'slash_cmd' for built-in slash-command output.
    """
    url = _BASE.format(token=_token()) + "/sendMessage"
    chat_id: Optional[str] = None
    sent: Optional[dict] = None
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            chat_id = await _chat_id()
            payload: dict = {"chat_id": chat_id, "text": text}
            if parse_mode is not None:
                payload["parse_mode"] = parse_mode
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            r = await client.post(url, json=payload)
            if r.status_code == 400 and parse_mode == "Markdown":
                # Most common cause: stray markdown specials in `text` (backticks,
                # unbalanced *_, etc.) tripped Telegram's parser. Log the body so
                # the cause is visible, then retry once as plain text so the
                # message still gets through.
                log.warning("Telegram 400 with Markdown — body=%s; retrying plain text", r.text[:300])
                payload.pop("parse_mode", None)
                r = await client.post(url, json=payload)
            if r.status_code >= 400:
                log.error("Telegram send failed: HTTP %d — body=%s", r.status_code, r.text[:300])
            else:
                sent = r.json()
        except Exception as exc:
            log.error("Telegram send failed: %s", exc)

    # Log to telegram_message even on send-failure: the LLM's reply intent is
    # still part of its conversation history, and an audit trail of attempted
    # sends is useful. Only skip if we couldn't even resolve a chat_id.
    if chat_id is not None or sent is not None:
        await _log_outbound_safe(
            chat_id, kind, text,
            role=role, tool_calls=tool_calls, meta=meta,
        )
    return sent


async def send_photo(
    image_path: str,
    caption: Optional[str] = None,
    *,
    kind: str = "push",
    meta: Optional[dict] = None,
) -> Optional[dict]:
    """Send an image (PNG/JPG) via Telegram sendPhoto. Returns response dict or None on failure.

    Logs to telegram_message as kind='push' by default (charts/digests are
    typically pushes). The logged `content` is the caption plus a tag noting
    the image filename so an audit query can identify which chart was sent.
    """
    from pathlib import Path as _Path
    url = _BASE.format(token=_token()) + "/sendPhoto"
    p = _Path(image_path)
    if not p.exists():
        log.error("Telegram sendPhoto: file not found at %s", image_path)
        return None
    chat_id: Optional[str] = None
    sent: Optional[dict] = None
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            chat_id = await _chat_id()
            data: dict = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption[:1024]
            with open(p, "rb") as f:
                mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
                files = {"photo": (p.name, f, mime)}
                r = await client.post(url, data=data, files=files)
            if r.status_code >= 400:
                log.error("Telegram sendPhoto failed: HTTP %d — %s", r.status_code, r.text[:300])
            else:
                sent = r.json()
        except Exception as exc:
            log.error("Telegram sendPhoto failed: %s", exc)

    photo_meta = dict(meta or {})
    photo_meta.setdefault("image_path", str(p))
    content = caption or f"[photo: {p.name}]"
    if chat_id is not None or sent is not None:
        await _log_outbound_safe(chat_id, kind, content, meta=photo_meta)
    return sent


async def poll_for_reply(after_message_id: int, timeout_s: int = 120) -> Optional[str]:
    """Long-poll for a user reply after a given message_id. Returns the reply text or None."""
    url = _BASE.format(token=_token()) + "/getUpdates"
    deadline = asyncio.get_event_loop().time() + timeout_s
    offset = after_message_id + 1

    async with httpx.AsyncClient(timeout=35) as client:
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            poll_timeout = min(30, int(remaining))
            if poll_timeout <= 0:
                break
            try:
                r = await client.get(url, params={
                    "offset": offset,
                    "timeout": poll_timeout,
                    "allowed_updates": ["message"],
                })
                r.raise_for_status()
                data = r.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    text = msg.get("text", "").strip().lower()
                    if text in ("y", "yes", "n", "no"):
                        return text
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                log.error("Telegram poll error: %s", exc)
                await asyncio.sleep(2)

    return None
