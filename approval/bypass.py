"""Time-bounded approval bypass.

When active, every approval gate (order >= threshold and strategic
proposals) auto-approves without Telegram round-trip. State lives in
`data/auto_approve_until.json`:

    {"until_ts": <unix>, "reason": "...", "set_at": <unix>}

Two consumers honor it:
  - `approval.workflow.request_approval` — returns approved=True immediately
  - `approval.proposals.create` — marks status="approved" at creation,
    sends a different Telegram ping ("auto-approved (bypass mode)")

The audit trail is preserved either way: the proposal still appears in
`pending_proposals.json` with `resolved_reason="bypass mode (...)"`, and
order rows still record `human_approved=True` with `bypass mode` in the
reason column.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

_STORE = Path("data/auto_approve_until.json")


def _load() -> dict | None:
    if not _STORE.exists():
        return None
    try:
        return json.loads(_STORE.read_text(encoding="utf-8"))
    except Exception:
        return None


def is_active() -> bool:
    s = _load()
    if not s:
        return False
    return float(s.get("until_ts", 0)) > time.time()


def reason() -> str:
    s = _load() or {}
    until = float(s.get("until_ts", 0))
    if until <= time.time():
        return ""
    expires_local = datetime.fromtimestamp(until).strftime("%H:%M")
    note = s.get("reason") or ""
    return f"bypass mode (expires {expires_local}{(' — ' + note) if note else ''})"


def status() -> dict:
    s = _load()
    if not s:
        return {"active": False}
    until = float(s.get("until_ts", 0))
    return {
        "active": until > time.time(),
        "until_ts": until,
        "until_local": datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M:%S"),
        "remaining_s": max(0, int(until - time.time())),
        "reason": s.get("reason", ""),
        "set_at": s.get("set_at"),
    }


def enable(hours: float, reason_note: str = "") -> dict:
    until = time.time() + hours * 3600.0
    payload = {
        "until_ts": until,
        "reason": reason_note,
        "set_at": time.time(),
    }
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    _STORE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return status()


def disable() -> None:
    try:
        _STORE.unlink()
    except FileNotFoundError:
        pass
