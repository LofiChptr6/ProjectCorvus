"""Strategic-change proposal tracker.

A proposal is a pending decision that needs human approval via Telegram.
Proposals persist to `data/pending_proposals.json` so a separate nudge
routine can re-ping every 5 minutes until the user replies "y" or "n".
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from approval.telegram import _BASE, _token, send_message

import httpx

log = logging.getLogger(__name__)

_STORE = Path("data/pending_proposals.json")
_CONCIERGE_LOCK = Path("data/concierge.lock")
NUDGE_INTERVAL_S = 300  # 5 minutes


def _concierge_alive() -> bool:
    """Return True iff data/concierge.lock points to a live PID.

    If the concierge is up, it owns Telegram getUpdates and this module must
    not poll — otherwise the two processes race on the offset counter.
    """
    if not _CONCIERGE_LOCK.exists():
        return False
    try:
        pid = int(_CONCIERGE_LOCK.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import subprocess
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}"],
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return str(pid).encode() in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _load() -> list[dict]:
    if not _STORE.exists():
        return []
    try:
        return json.loads(_STORE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(proposals: list[dict]) -> None:
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    _STORE.write_text(json.dumps(proposals, indent=2), encoding="utf-8")


def _fmt_message(p: dict, nudge: bool = False) -> str:
    prefix = "🔔 *Re-pinging (no reply yet)*" if nudge else "📝 *Strategic proposal — approval needed*"
    return (
        f"{prefix}\n\n"
        f"*ID:* `{p['id'][:8]}`\n"
        f"*Title:* {p['title']}\n\n"
        f"{p['details']}\n\n"
        f"Reply `y {p['id'][:8]}` to approve or `n {p['id'][:8]}` to reject.\n"
        f"(Or just `y` / `n` for the oldest open proposal.)"
    )


async def create(title: str, details: str) -> dict:
    """Create a new pending proposal and send initial Telegram ping."""
    proposal = {
        "id": str(uuid.uuid4()),
        "title": title,
        "details": details,
        "created_at": time.time(),
        "last_pinged_at": time.time(),
        "ping_count": 1,
        "status": "pending",  # pending | approved | rejected
        "resolved_at": None,
        "resolved_reason": None,
    }
    proposals = _load()
    proposals.append(proposal)
    _save(proposals)

    await send_message(_fmt_message(proposal, nudge=False))
    log.info("Created proposal id=%s title=%s", proposal["id"][:8], title)
    return proposal


def list_pending() -> list[dict]:
    return [p for p in _load() if p["status"] == "pending"]


def list_all() -> list[dict]:
    return _load()


async def nudge_stale() -> int:
    """Re-ping any pending proposals whose last_pinged_at is > NUDGE_INTERVAL_S ago.
    Returns the number of nudges sent."""
    proposals = _load()
    now = time.time()
    nudged = 0
    for p in proposals:
        if p["status"] != "pending":
            continue
        if now - p["last_pinged_at"] < NUDGE_INTERVAL_S:
            continue
        await send_message(_fmt_message(p, nudge=True))
        p["last_pinged_at"] = now
        p["ping_count"] += 1
        nudged += 1
        log.info("Nudged proposal id=%s (ping #%d)", p["id"][:8], p["ping_count"])
    if nudged:
        _save(proposals)
    return nudged


def _resolve(proposal_id_prefix: str, approved: bool, reason: str = "") -> Optional[dict]:
    proposals = _load()
    for p in proposals:
        if p["status"] == "pending" and p["id"].startswith(proposal_id_prefix):
            p["status"] = "approved" if approved else "rejected"
            p["resolved_at"] = time.time()
            p["resolved_reason"] = reason
            _save(proposals)
            return p
    return None


def _resolve_oldest(approved: bool, reason: str = "") -> Optional[dict]:
    proposals = _load()
    pending = sorted(
        [p for p in proposals if p["status"] == "pending"],
        key=lambda x: x["created_at"],
    )
    if not pending:
        return None
    p = pending[0]
    p["status"] = "approved" if approved else "rejected"
    p["resolved_at"] = time.time()
    p["resolved_reason"] = reason
    _save(proposals)
    return p


async def process_inbox() -> dict:
    """Poll Telegram getUpdates and classify each new message:
      - 'y'/'yes'/'n'/'no' (optionally with a proposal short-id) → resolve proposal
      - anything else → return as a free-text user command to be acted on

    Also nudges any pending proposals older than NUDGE_INTERVAL_S.
    Returns: {resolved, commands, nudged, pending}.

    Coexistence: if the concierge daemon is running (data/concierge.lock held by
    a live PID), it owns Telegram getUpdates. Calling this from a scheduled
    Claude Code command while the concierge is live would fight for the offset,
    so we return a no-op status and let the concierge handle everything.
    """
    if _concierge_alive():
        return {
            "resolved": [],
            "commands": [],
            "nudged": 0,
            "pending": len(list_pending()),
            "concierge_online": True,
            "delegated": True,
            "note": "Concierge daemon is polling Telegram — no action taken here.",
        }

    offset_path = Path("data/telegram_update_offset.txt")
    offset = 0
    if offset_path.exists():
        try:
            offset = int(offset_path.read_text(encoding="utf-8").strip())
        except Exception:
            offset = 0

    url = _BASE.format(token=_token()) + "/getUpdates"
    resolved = []
    commands = []  # free-text messages the user sent
    new_offset = offset

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(url, params={"offset": offset, "timeout": 0, "allowed_updates": ["message"]})
            r.raise_for_status()
            data = r.json()
            for update in data.get("result", []):
                new_offset = update["update_id"] + 1
                msg = update.get("message") or {}
                raw_text = (msg.get("text") or "").strip()
                if not raw_text:
                    continue
                text = raw_text.lower()
                parts = text.split()
                verdict = parts[0]
                if verdict in ("y", "yes", "n", "no"):
                    approved = verdict in ("y", "yes")
                    if len(parts) >= 2:
                        prop = _resolve(parts[1], approved, reason=f"Telegram reply: {text}")
                    else:
                        prop = _resolve_oldest(approved, reason=f"Telegram reply: {text}")
                    if prop:
                        resolved.append({"id": prop["id"][:8], "title": prop["title"], "approved": approved})
                        await send_message(
                            f"{'✅ Approved' if approved else '❌ Rejected'}: `{prop['id'][:8]}` — {prop['title']}"
                        )
                    else:
                        # y/n with no pending proposal — treat as a stray, ignore
                        pass
                else:
                    commands.append({
                        "text": raw_text,
                        "message_id": msg.get("message_id"),
                        "from": (msg.get("from") or {}).get("username") or (msg.get("from") or {}).get("first_name"),
                        "date": msg.get("date"),
                    })
        except Exception as exc:
            log.error("process_inbox failed: %s", exc)

    if new_offset != offset:
        offset_path.parent.mkdir(parents=True, exist_ok=True)
        offset_path.write_text(str(new_offset), encoding="utf-8")

    nudged = await nudge_stale()
    return {"resolved": resolved, "commands": commands, "nudged": nudged, "pending": len(list_pending())}
