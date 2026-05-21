"""Concierge main entry — long-running Telegram poller.

Run with:  python -m concierge.service

Owns Telegram getUpdates (single-poller invariant), routes inbound messages,
and periodically nudges stale proposals.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from dotenv import load_dotenv

from approval import proposals
from approval.telegram import _BASE, _token, send_message
from concierge import router, state

log = logging.getLogger("concierge")

_LOCK_PATH = Path("data/concierge.lock")
_OFFSET_PATH = Path("data/telegram_update_offset.txt")


# ── Lockfile (prevent double-start) ───────────────────────────────────────────


class _LockHeld(RuntimeError):
    pass


def acquire_lock() -> None:
    """Write our PID to data/concierge.lock. Fail if a live PID already owns it."""
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _LOCK_PATH.exists():
        try:
            existing = int(_LOCK_PATH.read_text(encoding="utf-8").strip())
        except Exception:
            existing = 0
        if existing and _pid_alive(existing):
            raise _LockHeld(
                f"Concierge already running as PID {existing}. "
                f"If this is stale, delete {_LOCK_PATH}."
            )
    _LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")


def release_lock() -> None:
    try:
        if _LOCK_PATH.exists():
            existing = int(_LOCK_PATH.read_text(encoding="utf-8").strip() or "0")
            if existing == os.getpid():
                _LOCK_PATH.unlink()
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ── Telegram offset handling ──────────────────────────────────────────────────


def _read_offset() -> int:
    if not _OFFSET_PATH.exists():
        return 0
    try:
        return int(_OFFSET_PATH.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        return 0


def _write_offset(offset: int) -> None:
    _OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OFFSET_PATH.write_text(str(offset), encoding="utf-8")


# ── Config ────────────────────────────────────────────────────────────────────


def _load_concierge_cfg() -> dict[str, Any]:
    p = Path("config.yaml")
    if not p.exists():
        return {}
    with open(p) as f:
        data = yaml.safe_load(f) or {}
    return dict(data.get("concierge") or {})


# ── Allowed chat ACL ──────────────────────────────────────────────────────────


def _allowed_chat_id() -> Optional[str]:
    """Return the chat_id we accept messages from, or None to allow any."""
    env_val = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if env_val:
        return env_val
    cache = Path("data/telegram_chat_id.txt")
    if cache.exists():
        return cache.read_text(encoding="utf-8").strip() or None
    return None


# ── Poll loop ─────────────────────────────────────────────────────────────────


async def _poll_once(client: httpx.AsyncClient, offset: int) -> tuple[int, list[dict[str, Any]]]:
    """One long-poll call. Returns (new_offset, events_to_handle).

    Events are dicts with a "type" discriminator: "message" (free-text/slash) or
    "callback" (inline-button tap on a proposal ping). The router branches on
    type — text goes through concierge.router; callbacks resolve a proposal in
    place and answer the loading spinner.
    """
    url = _BASE.format(token=_token()) + "/getUpdates"
    params = {"offset": offset, "timeout": 25, "allowed_updates": '["message","callback_query"]'}
    try:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as exc:
        log.warning("getUpdates failed: %s", exc)
        return offset, []

    allowed_chat = _allowed_chat_id()
    new_offset = offset
    events: list[dict[str, Any]] = []
    for update in data.get("result", []):
        new_offset = update["update_id"] + 1

        cq = update.get("callback_query")
        if cq:
            cb_data = (cq.get("data") or "").strip()
            cb_msg = cq.get("message") or {}
            chat_id = str((cb_msg.get("chat") or {}).get("id") or "")
            if allowed_chat and chat_id and chat_id != allowed_chat:
                log.warning("Dropped callback from unauthorized chat_id=%s data=%r", chat_id, cb_data[:80])
                continue
            events.append({
                "type": "callback",
                "data": cb_data,
                "chat_id": chat_id,
                "callback_query_id": cq.get("id"),
                "message_id": cb_msg.get("message_id"),
            })
            continue

        msg = update.get("message") or update.get("edited_message") or {}
        if not msg:
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        if allowed_chat and chat_id and chat_id != allowed_chat:
            log.warning("Dropped message from unauthorized chat_id=%s text=%r", chat_id, text[:80])
            continue
        # Telegram Bot API 7.0+: when the user long-presses a past message and
        # replies (optionally highlighting a fragment), the update carries
        # `reply_to_message` (the full original Message) and optionally `quote`
        # (the highlighted slice). The reply-aware concierge joins
        # reply_to.message_id back to our outbound row → source_ref.
        reply_to = msg.get("reply_to_message") or {}
        reply_to_id = reply_to.get("message_id") if isinstance(reply_to, dict) else None
        quote = msg.get("quote") or {}
        quote_text = quote.get("text") if isinstance(quote, dict) else None
        events.append({
            "type": "message",
            "text": text,
            "chat_id": chat_id,
            "message_id": msg.get("message_id"),
            "reply_to_message_id": reply_to_id,
            "quote_text": quote_text,
        })
    return new_offset, events


async def _answer_callback(client: httpx.AsyncClient, callback_query_id: str, text: str = "") -> None:
    """Acknowledge a callback_query so Telegram dismisses the button's spinner."""
    if not callback_query_id:
        return
    url = _BASE.format(token=_token()) + "/answerCallbackQuery"
    payload: dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text[:200]  # Telegram caps at 200 chars
    try:
        await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        log.warning("answerCallbackQuery failed: %s", exc)


async def _handle_callback(client: httpx.AsyncClient, ev: dict[str, Any]) -> None:
    """Resolve a proposal in response to an inline-button tap.

    callback_data shape (set in approval/proposals._proposal_buttons):
        approve_<short8>  |  reject_<short8>
    """
    from approval import proposals

    data = ev.get("data") or ""
    cq_id = ev.get("callback_query_id") or ""
    if data.startswith("approve_"):
        short, approved = data[len("approve_"):], True
    elif data.startswith("reject_"):
        short, approved = data[len("reject_"):], False
    else:
        log.warning("Unrecognised callback_data: %r", data[:80])
        await _answer_callback(client, cq_id, "Unknown action.")
        return

    p = proposals._resolve(short, approved, reason=f"Telegram button: {data}")
    if not p:
        await _answer_callback(client, cq_id, "No matching pending proposal.")
        # Log the inbound callback first (audit trail), then the response.
        try:
            from db import store
            await store.log_inbound(
                ev.get("chat_id"), None, "approval", data,
                meta={"event": "button_tap_no_match", "callback_data": data},
            )
        except Exception:
            log.debug("log_inbound (callback no-match) skipped", exc_info=True)
        await send_message(
            f"⚠️ Button tap for `{short}` — no matching pending proposal.",
            kind="approval",
            meta={"event": "button_no_match", "short_id": short, "callback_data": data},
            source_ref={"kind": "proposal", "event": "button_no_match",
                        "short_id": short, "callback_data": data},
        )
        return

    try:
        from db import store
        await store.log_inbound(
            ev.get("chat_id"), None, "approval", data,
            meta={"event": "button_tap", "callback_data": data, "short_id": p["id"][:8], "approved": approved},
        )
    except Exception:
        log.debug("log_inbound (callback) skipped", exc_info=True)

    verb = "Approved" if approved else "Rejected"
    icon = "✅" if approved else "❌"
    await _answer_callback(client, cq_id, f"{verb}")
    await send_message(
        f"{icon} {verb}: `{p['id'][:8]}` — {p['title']}",
        kind="approval",
        meta={"event": "resolved_via_button", "short_id": p["id"][:8], "approved": approved},
        source_ref={"kind": "proposal", "proposal_id": p["id"],
                    "proposal_kind": p.get("kind", "strategic_change"),
                    "title": p.get("title"),
                    "event": "resolved_via_button", "approved": approved},
    )


async def _nudge_loop(interval_s: int, stop_event: asyncio.Event) -> None:
    """Background task: re-ping stale proposals periodically."""
    while not stop_event.is_set():
        try:
            await proposals.nudge_stale()
        except Exception:
            log.exception("nudge_stale failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            continue


async def _run() -> None:
    cfg = _load_concierge_cfg()
    if not cfg.get("enabled", True):
        log.warning("Concierge disabled in config.yaml; exiting.")
        return

    base_url = os.environ.get("LOCAL_LLM_BASE_URL", "").strip() or "http://localhost:8000/v1"
    if not base_url:
        log.error("LOCAL_LLM_BASE_URL not set — exiting.")
        await send_message(
            "⚠️ Concierge cannot start: LOCAL_LLM_BASE_URL is missing in .env.",
            parse_mode=None,
            kind="push",
            meta={"author_agent": "system", "event": "concierge_startup_missing_env"},
            source_ref={"kind": "system_alert", "alert_kind": "concierge_lifecycle",
                        "author_agent": "system",
                        "event": "startup_missing_env"},
        )
        return
    if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        log.error("TELEGRAM_BOT_TOKEN not set — exiting.")
        return

    stop_event = asyncio.Event()

    def _signal_handler(*_a):
        log.info("Shutdown signal received")
        stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    await send_message(
        "🤖 Concierge online. Ask me anything, or /help for commands.",
        parse_mode=None,
        kind="push",
        meta={"author_agent": "system", "event": "concierge_online"},
        source_ref={"kind": "system_alert", "alert_kind": "concierge_lifecycle",
                    "author_agent": "system", "event": "online"},
    )

    nudge_interval = int(cfg.get("nudge_interval_s", 60))
    nudge_task = asyncio.create_task(_nudge_loop(nudge_interval, stop_event))

    offset = _read_offset()
    start_time = time.time()
    log.info("Concierge loop starting (offset=%d, model=%s)", offset, cfg.get("model", "default"))

    async with httpx.AsyncClient(timeout=35) as client:
        while not stop_event.is_set():
            try:
                new_offset, events = await _poll_once(client, offset)
            except Exception:
                log.exception("poll_once crashed — sleeping 5s")
                await asyncio.sleep(5)
                continue

            if new_offset != offset:
                offset = new_offset
                _write_offset(offset)

            for ev in events:
                try:
                    if ev["type"] == "callback":
                        await _handle_callback(client, ev)
                    else:
                        await router.route(
                            ev["text"], cfg,
                            chat_id=ev.get("chat_id"),
                            telegram_message_id=ev.get("message_id"),
                            reply_to_message_id=ev.get("reply_to_message_id"),
                            quote_text=ev.get("quote_text"),
                        )
                except Exception as exc:
                    log.exception("Router crashed on event: %r", ev)
                    try:
                        await send_message(
                            f"⚠️ Concierge hit an error: {type(exc).__name__}: {exc}",
                            parse_mode=None,
                            kind="push",
                            meta={"author_agent": "system", "event": "concierge_router_crash"},
                            source_ref={"kind": "system_alert",
                                        "alert_kind": "concierge_lifecycle",
                                        "author_agent": "system",
                                        "event": "router_crash",
                                        "error": f"{type(exc).__name__}: {exc}"[:200]},
                        )
                    except Exception:
                        pass

            # Tiny breather when there were no events (long-poll already waited 25s).
            if not events:
                await asyncio.sleep(0.2)

    stop_event.set()
    try:
        await asyncio.wait_for(nudge_task, timeout=3)
    except asyncio.TimeoutError:
        nudge_task.cancel()

    uptime_h = (time.time() - start_time) / 3600.0
    u = state.load_usage()
    try:
        await send_message(
            f"🤖 Concierge shutting down — {uptime_h:.1f}h uptime, "
            f"{u['requests']} LLM requests, "
            f"{u.get('input_tokens', 0) + u.get('output_tokens', 0):,} tokens today.",
            parse_mode=None,
            kind="push",
            meta={"author_agent": "system", "event": "concierge_offline"},
            source_ref={"kind": "system_alert", "alert_kind": "concierge_lifecycle",
                        "author_agent": "system", "event": "offline",
                        "uptime_h": round(uptime_h, 1)},
        )
    except Exception:
        pass


def _setup_logging() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "concierge.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handlers: list[logging.Handler] = [handler]
    # Detached runs (start_concierge_bg.sh) redirect stderr into concierge.log,
    # so a StreamHandler would double every line. Only add the console handler
    # when running interactively.
    if sys.stderr.isatty():
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handlers.append(console)
    logging.basicConfig(level=logging.INFO, handlers=handlers)


def main() -> int:
    load_dotenv()
    _setup_logging()
    try:
        acquire_lock()
    except _LockHeld as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        asyncio.run(_run())
        return 0
    except KeyboardInterrupt:
        log.info("Interrupted")
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
