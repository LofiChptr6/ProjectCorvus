"""News-feed search with evidence stamping.

Phase A of CITATION_ARCH. Where `search_posts` is a general ILIKE substring
search, `query_news` is the narrower 'find evidence in news-headlines for a
specific claim' query. Returns post_ids + snippets + an evidence_id that the
caller pins to a Citation(kind='news_post').

Design choices:
  - Search confined to the `news-headlines` thread by default. The other
    threads (agent posts, decisions) aren't sources of fact about the world.
  - AND-semantics across `terms`: all terms must appear in title or body.
    OR-semantics would be too permissive and let agents pin to weak matches.
  - Symbol filter is term-matched, not exact: 'WMB' might appear as
    'Williams Companies (WMB)' — substring is the right semantic here.
  - Hard limit + sort by posted_at DESC so the most recent match comes first.
  - Evidence row stores the QUERY + the matched post ids, not the post bodies
    (those live in the post table; the audit trail dereferences them on demand).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

TOOL_VERSION = "0.1.0"
_DEFAULT_THREAD = "news-headlines"
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


async def execute(
    terms: list[str],
    *,
    symbol: Optional[str] = None,
    window_days: int = 7,
    around_date: Optional[str] = None,
    thread_slug: str = _DEFAULT_THREAD,
    limit: int = _DEFAULT_LIMIT,
    agent_name: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Search the news-headlines thread for posts matching ALL `terms`,
    optionally constrained by a symbol mention and a date window.

    Args:
        terms: list of strings, ALL must appear (case-insensitive) in title or body
        symbol: if set, posts must also mention the ticker symbol
        window_days: search posts within this many days of `around_date`
                     (or now if around_date is None)
        around_date: ISO date 'YYYY-MM-DD' anchor (defaults to today)
        thread_slug: defaults to 'news-headlines'; rarely overridden
        limit: max posts returned (capped at 100)
        agent_name: stamped onto evidence row
        session_id: stamped onto evidence row

    Returns:
        {
          "ok": True,
          "terms": [...],
          "symbol": "...",
          "window": {"from": iso, "to": iso},
          "match_count": <int>,
          "matches": [{"post_id": int, "posted_at": iso, "title": str, "snippet": str}],
          "evidence_id": <int>,
        }
        Empty matches still return ok=True (an empty result is legitimate
        evidence of absence, which is what verify_catalyst exploits).
    """
    if not isinstance(terms, list) or not terms:
        return {"ok": False, "reason": "terms must be a non-empty list"}
    terms = [t.strip() for t in terms if t and t.strip()]
    if not terms:
        return {"ok": False, "reason": "terms must contain at least one non-empty string"}
    limit = max(1, min(int(limit), _MAX_LIMIT))
    window_days = max(1, min(int(window_days), 90))

    if around_date:
        try:
            anchor = datetime.fromisoformat(around_date).replace(tzinfo=timezone.utc)
        except ValueError:
            return {"ok": False, "reason": f"around_date must be ISO YYYY-MM-DD, got {around_date!r}"}
    else:
        anchor = datetime.now(timezone.utc)
    win_start = anchor - timedelta(days=window_days)
    win_end = anchor + timedelta(days=window_days)

    # Build ILIKE clauses: one per term + optional symbol filter, all AND-ed.
    clauses = ["t.slug = $1"]
    params: list = [thread_slug]
    clauses.append(f"p.posted_at >= ${len(params)+1}")
    params.append(win_start)
    clauses.append(f"p.posted_at <= ${len(params)+1}")
    params.append(win_end)
    for term in terms:
        clauses.append(f"(p.title ILIKE ${len(params)+1} OR p.body ILIKE ${len(params)+1})")
        params.append(f"%{term}%")
    if symbol:
        clauses.append(f"(p.title ILIKE ${len(params)+1} OR p.body ILIKE ${len(params)+1})")
        params.append(f"%{symbol.upper()}%")
    where = " AND ".join(clauses)
    params.append(limit)

    from db.schema import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT p.id, p.posted_at, p.author, p.title, p.body
                FROM post p JOIN thread t ON t.id = p.thread_id
                WHERE {where}
                ORDER BY p.posted_at DESC
                LIMIT ${len(params)}""",
            *params,
        )

    matches = []
    for r in rows:
        body = (r["body"] or "")
        snippet = body[:240] + ("…" if len(body) > 240 else "")
        matches.append({
            "post_id": int(r["id"]),
            "posted_at": r["posted_at"].isoformat(),
            "author": r["author"],
            "title": r["title"],
            "snippet": snippet,
        })

    # Evidence row: deterministic id from the query payload + match set.
    # Same query + same matches → same evidence_id (re-running this tool is
    # cheap and reproducible).
    from db import store
    source_ref = (
        f"news:{thread_slug}:{'+'.join(sorted(terms))[:60]}"
        f":{symbol or '-'}:{win_start.date().isoformat()}:{win_end.date().isoformat()}"
    )
    evidence_id = await store.stamp_evidence(
        kind="news_post" if matches else "news_post",  # always news_post kind for absence-of-evidence too
        source_ref_id=source_ref,
        inputs_json={
            "terms": terms,
            "symbol": symbol,
            "window_days": window_days,
            "around_date": anchor.date().isoformat(),
            "thread_slug": thread_slug,
        },
        outputs_json={
            "match_count": len(matches),
            "matched_post_ids": [m["post_id"] for m in matches],
        },
        content_snippet=(
            f"news search {terms!r} in {thread_slug} ±{window_days}d around "
            f"{anchor.date().isoformat()}: {len(matches)} matches"
        ),
        computed_by=f"query_news@{TOOL_VERSION}",
        agent_name=agent_name,
        session_id=session_id,
    )

    return {
        "ok": True,
        "terms": terms,
        "symbol": symbol,
        "window": {
            "from": win_start.date().isoformat(),
            "to": win_end.date().isoformat(),
        },
        "match_count": len(matches),
        "matches": matches,
        "evidence_id": evidence_id,
    }
