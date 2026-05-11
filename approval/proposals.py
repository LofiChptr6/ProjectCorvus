"""Strategic-change proposal tracker.

A proposal is a pending decision that needs human approval via Telegram.
Proposals persist to `data/pending_proposals.json` so a separate nudge
routine can re-ping every 5 minutes until the user replies "y" or "n".
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

from approval.telegram import send_message

log = logging.getLogger(__name__)

_STORE = Path("data/pending_proposals.json")
NUDGE_INTERVAL_S = 300  # 5 minutes


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
        f"Tap a button below, or reply `/y` / `/n` for the oldest pending."
    )


def _proposal_buttons(p: dict) -> dict:
    """Inline-keyboard markup with one-tap approve/reject for this proposal."""
    short = p["id"][:8]
    return {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve_{short}"},
            {"text": "❌ Reject",  "callback_data": f"reject_{short}"},
        ]]
    }


async def create(title: str, details: str) -> dict:
    """Create a new pending proposal and send initial Telegram ping.

    If bypass mode is active, the proposal is created in `approved` state
    immediately (audit trail preserved) and a different Telegram ping is
    sent. No nudge loop runs for auto-approved proposals."""
    from approval import bypass

    now = time.time()
    auto_approved = bypass.is_active()
    bypass_reason = bypass.reason() if auto_approved else None

    proposal = {
        "id": str(uuid.uuid4()),
        "title": title,
        "details": details,
        "created_at": now,
        "last_pinged_at": now,
        "ping_count": 1,
        "status": "approved" if auto_approved else "pending",
        "resolved_at": now if auto_approved else None,
        "resolved_reason": bypass_reason if auto_approved else None,
    }
    proposals = _load()
    proposals.append(proposal)
    _save(proposals)

    proposal_meta = {"proposal_id": proposal["id"], "short_id": proposal["id"][:8]}
    if auto_approved:
        await send_message(
            f"⚡ *Strategic proposal auto-approved* ({bypass_reason})\n\n"
            f"*ID:* `{proposal['id'][:8]}`\n"
            f"*Title:* {title}\n\n"
            f"{details}",
            kind="approval",
            meta={**proposal_meta, "event": "auto_approved"},
        )
        log.info("Auto-approved proposal id=%s title=%s (%s)",
                 proposal["id"][:8], title, bypass_reason)
    else:
        await send_message(
            _fmt_message(proposal, nudge=False),
            reply_markup=_proposal_buttons(proposal),
            kind="approval",
            meta={**proposal_meta, "event": "created"},
        )
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
        await send_message(
            _fmt_message(p, nudge=True),
            reply_markup=_proposal_buttons(p),
            kind="approval",
            meta={"proposal_id": p["id"], "short_id": p["id"][:8], "event": "nudge"},
        )
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


def bulk_resolve(approved: bool, reason: str = "") -> list[dict]:
    """Resolve every pending proposal in one shot. Returns a list of
    {short_id, title, status} entries for the proposals that were resolved.

    Used by the concierge's `resolve_all_pending` tool. Does NOT send Telegram
    confirmations — the caller composes a single summary message after the user
    confirms the bulk action. No-op (returns []) if no proposals are pending.
    """
    proposals = _load()
    out: list[dict] = []
    now = time.time()
    new_status = "approved" if approved else "rejected"
    for p in proposals:
        if p["status"] != "pending":
            continue
        p["status"] = new_status
        p["resolved_at"] = now
        p["resolved_reason"] = reason
        out.append({"short_id": p["id"][:8], "title": p["title"], "status": p["status"]})
    if out:
        _save(proposals)
    return out


def list_recent_decisions(limit: int = 20) -> list[dict]:
    """Return the most-recently resolved proposals (approved or rejected),
    newest first. Read-only — used by the concierge to answer questions like
    'did I approve the pause on Vera?' without trawling chat history."""
    proposals = _load()
    decided = [p for p in proposals if p.get("status") in ("approved", "rejected")]
    decided.sort(key=lambda p: p.get("resolved_at") or 0, reverse=True)
    out: list[dict] = []
    for p in decided[:max(0, int(limit))]:
        out.append({
            "short_id": p["id"][:8],
            "title": p["title"],
            "status": p["status"],
            "resolved_at": p.get("resolved_at"),
            "resolved_reason": p.get("resolved_reason"),
        })
    return out


