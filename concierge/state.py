"""Concierge persistent state — conversation history and usage tracking.

All state is JSON on disk. Atomic writes via temp-file-then-rename.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_CHAT_PATH = Path("data/concierge_chat.json")
_USAGE_PATH = Path("data/concierge_usage.json")
_PENDING_CONFIRM_PATH = Path("data/concierge_pending_confirm.json")

# Claude Sonnet 4.5 pricing (USD per 1M tokens) — update if API changes.
# Source: https://docs.anthropic.com/en/docs/about-claude/pricing
_USD_PER_M_INPUT = 3.00
_USD_PER_M_OUTPUT = 15.00
_USD_PER_M_CACHE_WRITE = 3.75
_USD_PER_M_CACHE_READ = 0.30


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use NamedTemporaryFile in the same dir to guarantee same-volume rename.
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Conversation history ──────────────────────────────────────────────────────


def load_history() -> list[dict[str, Any]]:
    if not _CHAT_PATH.exists():
        return []
    try:
        return json.loads(_CHAT_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not load concierge history: %s — starting fresh", exc)
        return []


def save_history(messages: list[dict[str, Any]]) -> None:
    _atomic_write(_CHAT_PATH, json.dumps(messages, indent=2, default=str))


def prune_history(messages: list[dict[str, Any]], max_turns: int) -> list[dict[str, Any]]:
    """Keep only the last `max_turns` user/assistant pairs.

    Each "turn" = one user message + one assistant response. Tool-result/tool-use
    pairs count toward the assistant half, so we keep them together with the
    user message that triggered them.
    """
    if len(messages) <= max_turns * 2:
        return messages
    # Find boundaries — every time we see role=user after the assistant finished,
    # that's the start of a new turn. Keep the tail.
    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "user" and current and current[0].get("role") == "user":
            # start of a new turn (previous turn's tool results still flowed into current)
            # Simpler heuristic: treat every user message that ISN'T a tool_result as a new turn.
            content = msg.get("content")
            is_tool_result = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            )
            if not is_tool_result:
                turns.append(current)
                current = [msg]
                continue
        if msg.get("role") == "user" and not current:
            current = [msg]
            continue
        current.append(msg)
    if current:
        turns.append(current)

    kept = turns[-max_turns:]
    flat: list[dict[str, Any]] = []
    for t in kept:
        flat.extend(t)
    return flat


# ── Usage / spend tracking ────────────────────────────────────────────────────


def _today_utc_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def load_usage() -> dict[str, Any]:
    if not _USAGE_PATH.exists():
        return {"date": _today_utc_iso(), "input_tokens": 0, "output_tokens": 0,
                "cache_write_tokens": 0, "cache_read_tokens": 0, "usd": 0.0, "requests": 0}
    try:
        data = json.loads(_USAGE_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if data.get("date") != _today_utc_iso():
        # Reset daily counter at UTC midnight.
        return {"date": _today_utc_iso(), "input_tokens": 0, "output_tokens": 0,
                "cache_write_tokens": 0, "cache_read_tokens": 0, "usd": 0.0, "requests": 0}
    return data


def save_usage(u: dict[str, Any]) -> None:
    _atomic_write(_USAGE_PATH, json.dumps(u, indent=2))


def record_usage(usage_obj: Any) -> dict[str, Any]:
    """Given an Anthropic Usage object, accumulate tokens + dollars for today."""
    u = load_usage()
    inp = getattr(usage_obj, "input_tokens", 0) or 0
    out = getattr(usage_obj, "output_tokens", 0) or 0
    cw = getattr(usage_obj, "cache_creation_input_tokens", 0) or 0
    cr = getattr(usage_obj, "cache_read_input_tokens", 0) or 0
    u["input_tokens"] += inp
    u["output_tokens"] += out
    u["cache_write_tokens"] += cw
    u["cache_read_tokens"] += cr
    u["requests"] += 1
    u["usd"] += (inp / 1_000_000.0) * _USD_PER_M_INPUT
    u["usd"] += (out / 1_000_000.0) * _USD_PER_M_OUTPUT
    u["usd"] += (cw / 1_000_000.0) * _USD_PER_M_CACHE_WRITE
    u["usd"] += (cr / 1_000_000.0) * _USD_PER_M_CACHE_READ
    save_usage(u)
    return u


def budget_exceeded(cap_usd: float) -> bool:
    if cap_usd <= 0:
        return False
    return load_usage()["usd"] >= cap_usd


# ── Pending write-action confirmations ────────────────────────────────────────
#
# When Sonnet wants to take a write action (resolve_proposal, propose_strategic_change),
# we stash the intent here and ask the user to reply YES. The next inbound message
# is matched against this file; if confirmed, the action runs.


def load_pending_confirm() -> dict[str, Any] | None:
    if not _PENDING_CONFIRM_PATH.exists():
        return None
    try:
        return json.loads(_PENDING_CONFIRM_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_pending_confirm(intent: dict[str, Any]) -> None:
    _atomic_write(_PENDING_CONFIRM_PATH, json.dumps(intent, indent=2, default=str))


def clear_pending_confirm() -> None:
    try:
        _PENDING_CONFIRM_PATH.unlink()
    except FileNotFoundError:
        pass
