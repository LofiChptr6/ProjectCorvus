"""Concierge persistent state — usage tracking + pending write-action confirmations.

Conversation history moved to the `telegram_message` Postgres table — see
`db.store.load_concierge_history`. The two pieces still here are small,
ephemeral, and don't benefit from DB storage:
  - daily token usage counter (resets at UTC midnight)
  - pending YES-to-confirm gate for staged write actions
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

_USAGE_PATH = Path("data/concierge_usage.json")
_PENDING_CONFIRM_PATH = Path("data/concierge_pending_confirm.json")


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


# ── Usage / spend tracking ────────────────────────────────────────────────────


def _today_utc_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _empty_usage() -> dict[str, Any]:
    return {"date": _today_utc_iso(), "input_tokens": 0, "output_tokens": 0, "requests": 0}


def load_usage() -> dict[str, Any]:
    if not _USAGE_PATH.exists():
        return _empty_usage()
    try:
        data = json.loads(_USAGE_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if data.get("date") != _today_utc_iso():
        return _empty_usage()
    # Backfill missing keys for forward-compat with old rows.
    for k, v in _empty_usage().items():
        data.setdefault(k, v)
    return data


def save_usage(u: dict[str, Any]) -> None:
    _atomic_write(_USAGE_PATH, json.dumps(u, indent=2))


def record_usage(usage_obj: Any) -> dict[str, Any]:
    """Accumulate token counts for today.

    Accepts either an OpenAI usage object (attrs prompt_tokens/completion_tokens)
    or a dict with the same keys. The legacy Anthropic shape (input_tokens/
    output_tokens) is also accepted so a partial cutover or replay of an old
    log file doesn't crash.
    """
    u = load_usage()

    def _get(name: str) -> int:
        if hasattr(usage_obj, name):
            return int(getattr(usage_obj, name) or 0)
        if isinstance(usage_obj, dict):
            return int(usage_obj.get(name) or 0)
        return 0

    inp = _get("prompt_tokens") or _get("input_tokens")
    out = _get("completion_tokens") or _get("output_tokens")
    u["input_tokens"] += inp
    u["output_tokens"] += out
    u["requests"] += 1
    save_usage(u)
    return u


def token_cap_exceeded(cap_tokens: int) -> bool:
    if cap_tokens <= 0:
        return False
    u = load_usage()
    return (u["input_tokens"] + u["output_tokens"]) >= cap_tokens


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
