"""Resolve the context behind a Telegram reply.

When the user long-presses a past bot message and replies to it, Telegram's
Bot API carries `reply_to_message.message_id` on the inbound update. The
concierge router captures that id and the highlighted `quote` (Bot API 7.0+),
then calls `resolve_reply_context()` here.

This module joins the inbound reply pointer back to the originating outbound
row in `telegram_message`, reads its `source_ref` JSONB, and dispatches on
`source_ref.kind` to load the right slice of agent state (recent theses,
proposal payload, conviction view, etc.) so `chat.handle` can splice it into
the LLM prompt as an explicit "Source context" block.

The bundle is recomputed every turn — never persisted — because agent state
is live and the snapshot we'd take at message-send time isn't what the user
is asking about now.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from db import store

log = logging.getLogger(__name__)


# How many open theses / recent posts to pull per resolved agent. The bundle
# ends up in every turn's prompt, so trim aggressively — the LLM has
# get_agent_overview / get_recent_telegram_pushes for follow-up depth.
_THESES_PER_AGENT = 5
_RESOLUTIONS_PER_AGENT = 3
_THREAD_POSTS_PER_AGENT = 3


async def resolve_reply_context(
    reply_to_telegram_message_id: int,
    quote_text: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Return a context bundle the LLM should use as primary frame for its
    answer, or None if the referenced message can't be found.

    The bundle always carries `original_text` (what the user replied to) and
    `quote_text` (the highlighted fragment, verbatim). Kind-specific fields:

    - agent_push:    author_agent + open_theses + active_convictions + last
                     thread posts by that author + the linked post body if
                     source_ref carried `post_id`.
    - proposal:      full proposal dict from `pending_proposals.json`.
    - trade_approval: symbol + active convictions on that symbol.
    - system_alert:  source_ref passed through (already self-describing).
    - chat_history:  source_ref was NULL (historical / conversational row);
                     bundle is just the original text + quote.
    """
    if not reply_to_telegram_message_id:
        return None

    pool = await store.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, content, created_at, source_ref, kind, role
               FROM telegram_message
               WHERE telegram_message_id = $1
                 AND direction = 'outbound'
               ORDER BY id DESC
               LIMIT 1""",
            int(reply_to_telegram_message_id),
        )

    if not row:
        return None

    src = row["source_ref"]
    if isinstance(src, str):
        try:
            src = json.loads(src)
        except json.JSONDecodeError:
            src = None
    src = src or {}

    bundle: dict[str, Any] = {
        "original_text": (row["content"] or "")[:2000],
        "sent_at": str(row["created_at"]),
        "outbound_row_id": int(row["id"]),
        "telegram_kind": row["kind"],
    }
    if quote_text:
        bundle["quote_text"] = quote_text[:1000]

    kind = src.get("kind")
    if kind == "agent_push":
        bundle.update(await _resolve_agent_push(src))
    elif kind == "proposal":
        bundle.update(await _resolve_proposal(src))
    elif kind == "trade_approval":
        bundle.update(await _resolve_trade_approval(src))
    elif kind == "system_alert":
        bundle.update(_resolve_system_alert(src))
    else:
        bundle["kind"] = "chat_history"
        if src:
            bundle["source_ref"] = src

    return bundle


# ── Kind-specific resolvers ───────────────────────────────────────────────────


async def _resolve_agent_push(src: dict[str, Any]) -> dict[str, Any]:
    """Pull author_agent's recent state. Best-effort — any single lookup
    failure leaves that field absent rather than aborting the whole bundle."""
    out: dict[str, Any] = {"kind": "agent_push"}
    agent = src.get("author_agent")
    out["author_agent"] = agent
    if src.get("subkind"):
        out["subkind"] = src["subkind"]

    if not agent or agent == "system":
        # System pushes (concierge lifecycle, daemon health) have no agent
        # state to pull — the original_text + source_ref carry it all.
        return out

    # Theses (open + recent resolutions)
    try:
        out["open_theses"] = await store.get_open_theses(agent, limit=_THESES_PER_AGENT)
    except Exception as exc:
        log.debug("get_open_theses(%s) failed: %s", agent, exc)
    try:
        out["recent_resolutions"] = await store.get_recent_resolutions(
            agent, limit=_RESOLUTIONS_PER_AGENT
        )
    except Exception as exc:
        log.debug("get_recent_resolutions(%s) failed: %s", agent, exc)

    # Active convictions
    try:
        out["active_convictions"] = await store.get_agent_active_convictions(agent)
    except Exception as exc:
        log.debug("get_agent_active_convictions(%s) failed: %s", agent, exc)

    # Recent thread posts by this author (any thread). Helps when the user
    # asks "why" about a sector summary — the long-form thread post often
    # carries the full rationale that the Telegram blurb truncated.
    try:
        out["recent_thread_posts"] = await _recent_posts_by_author(
            agent, limit=_THREAD_POSTS_PER_AGENT
        )
    except Exception as exc:
        log.debug("recent_thread_posts(%s) failed: %s", agent, exc)

    # Specific post linked from source_ref (when notify.py or similar
    # captured a post_id at send time). Future-proof — none of the current
    # callsites populate it, but the resolver supports it for when they do.
    post_id = src.get("post_id")
    if post_id:
        try:
            out["linked_post"] = await _get_post_by_id(int(post_id))
        except Exception as exc:
            log.debug("get_post_by_id(%s) failed: %s", post_id, exc)

    return out


async def _resolve_proposal(src: dict[str, Any]) -> dict[str, Any]:
    """Look up the proposal by id or short_id from pending_proposals.json."""
    out: dict[str, Any] = {"kind": "proposal"}
    pid = src.get("proposal_id")
    short = src.get("short_id")
    out["proposal_id"] = pid
    out["short_id"] = short or (pid[:8] if isinstance(pid, str) else None)
    if src.get("event"):
        out["event"] = src["event"]

    try:
        from approval import proposals
        all_props = proposals.list_all()
        match = None
        if pid:
            match = next((p for p in all_props if p.get("id") == pid), None)
        if match is None and short:
            match = next(
                (p for p in all_props if str(p.get("id", "")).startswith(short)),
                None,
            )
        if match:
            out["proposal"] = match
    except Exception as exc:
        log.debug("proposal lookup failed: %s", exc)
    return out


async def _resolve_trade_approval(src: dict[str, Any]) -> dict[str, Any]:
    """For a trade-approval message, pull the symbol's active convictions so
    the LLM can explain WHY the trade was queued."""
    out: dict[str, Any] = {"kind": "trade_approval"}
    for f in ("symbol", "action", "quantity", "agent_name", "stage", "notional", "mode"):
        if src.get(f) is not None:
            out[f] = src[f]
    symbol = (src.get("symbol") or "").upper()
    if symbol:
        try:
            out["active_convictions"] = await store.get_convictions_for_symbol(symbol)
        except Exception as exc:
            log.debug("get_convictions_for_symbol(%s) failed: %s", symbol, exc)
    return out


def _resolve_system_alert(src: dict[str, Any]) -> dict[str, Any]:
    """System alerts are self-describing — no extra DB lookups. Just expose
    the fields source_ref already carries."""
    out: dict[str, Any] = {"kind": "system_alert"}
    for f in ("alert_kind", "author_agent", "subject", "scope", "reason",
              "error", "event", "uptime_h", "activated_by"):
        if src.get(f) is not None:
            out[f] = src[f]
    return out


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _recent_posts_by_author(author: str, limit: int = 3) -> list[dict]:
    """Return the last `limit` thread posts authored by `author`, any thread,
    newest first. Bypasses the per-thread `get_posts` helper since we need
    cross-thread scope here."""
    pool = await store.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT p.id, p.thread_id, t.slug AS thread_slug, p.author,
                      p.posted_at, p.title, p.body, p.meta
               FROM post p
               JOIN thread t ON t.id = p.thread_id
               WHERE p.author = $1
                 AND (p.expires_at IS NULL OR p.expires_at > NOW())
               ORDER BY p.id DESC
               LIMIT $2""",
            author, max(1, min(int(limit), 20)),
        )
    return [dict(r) for r in rows]


async def _get_post_by_id(post_id: int) -> Optional[dict]:
    pool = await store.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT p.id, p.thread_id, t.slug AS thread_slug, p.author,
                      p.posted_at, p.title, p.body, p.meta
               FROM post p
               JOIN thread t ON t.id = p.thread_id
               WHERE p.id = $1""",
            int(post_id),
        )
    return dict(row) if row else None


def format_bundle_for_prompt(bundle: dict[str, Any]) -> str:
    """Render the resolved bundle as a compact text block to inject into the
    LLM's system context. JSON keeps it explicit + parseable for the model."""
    return json.dumps(bundle, default=str, indent=2)
