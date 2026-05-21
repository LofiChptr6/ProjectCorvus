"""Catalyst verification: does an alleged event-date actually appear in news?

Phase A of CITATION_ARCH. Designed for the failure mode the 2026-05-21 audit
uncovered: the energy agent cited 'OPEC+ meeting on June 4' as a `verify_by`
catalyst in 51 theses over 15 days, and zero news posts mention such an
event. `verify_catalyst` is the deterministic check that catches that pattern
at submission time.

The tool searches the news feed for posts in a window around the alleged date
that mention BOTH the event terms AND a date marker. Returns:
  - found = True with matches if the catalyst is in the feed
  - found = False with empty matches as 'absence of evidence' — that is
    itself a legitimate evidence row (the verifier worker uses it to reject
    theses citing fabricated catalysts).

Designed to be the predicate behind `Thesis.catalyst_source` validation:
no entry in this tool's evidence_snapshot → thesis can't be `kind='hypothesis'`
or `kind='prediction'`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

TOOL_VERSION = "0.1.0"


def _date_markers(target: datetime) -> list[str]:
    """Generate the calendar-string variants we'd expect news to use for a date.
    Example for 2026-06-04: ['June 4', 'Jun 4', '6/4', '06/04', '2026-06-04'].
    Each call to verify_catalyst checks for ANY of these in the matched bodies."""
    out = []
    d = target.date()
    month_full = target.strftime("%B")          # June
    month_abbr = target.strftime("%b")          # Jun
    day = target.day                            # 4 (no leading zero)
    out.append(f"{month_full} {day}")           # June 4
    out.append(f"{month_abbr} {day}")           # Jun 4
    out.append(f"{month_abbr}. {day}")          # Jun. 4
    out.append(f"{target.month}/{day}")         # 6/4
    out.append(f"{target.month:02d}/{day:02d}") # 06/04
    out.append(d.isoformat())                   # 2026-06-04
    return out


async def execute(
    event_text: str,
    *,
    date: str,
    lookback_days: int = 30,
    lookforward_days: int = 3,
    agent_name: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Verify that an alleged event happens on or near `date`.

    The search window is asymmetric on purpose: news coverage of FUTURE
    catalysts arrives weeks ahead ("OPEC meets June 4" is reported on May 10),
    so we look back further than forward. The literal date string appearing
    in any matched body raises confidence from 'window' to 'strong'.

    Args:
        event_text: free-form description, e.g. "OPEC+ meeting" or "FOMC announcement"
        date: ISO date 'YYYY-MM-DD' the catalyst is supposed to occur
        lookback_days: search posts back this many days from the date (default 30,
                       max 60) — catches advance coverage of future events
        lookforward_days: search posts forward this many days (default 3) —
                          catches same-day and day-after coverage of past events
        agent_name: stamped onto evidence row
        session_id: stamped onto evidence row

    Returns:
        {
          "ok": True,
          "event": "...",
          "date": "YYYY-MM-DD",
          "lookback_days": int,
          "lookforward_days": int,
          "found": bool,
          "confidence": "strong" | "window" | "absent",
          "matches": [{post_id, posted_at, title, snippet, matched_date_marker}],
          "checked_date_markers": [...],
          "evidence_id": <int>,
        }
        - `confidence='strong'`: news bodies explicitly name the date AND the event
        - `confidence='window'`: event topic is in the news but no body names the date
        - `confidence='absent'`: zero news mentions of the event → catalyst likely fabricated

        The verifier worker (Phase C) treats 'absent' as auto-reject and
        'window' as a downgrade signal for date-anchored thesis claims.
    """
    if not event_text or not event_text.strip():
        return {"ok": False, "reason": "event_text is required"}
    try:
        anchor = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
    except ValueError:
        return {"ok": False, "reason": f"date must be ISO YYYY-MM-DD, got {date!r}"}
    lookback_days = max(0, min(int(lookback_days), 60))
    lookforward_days = max(0, min(int(lookforward_days), 14))

    # Asymmetric window: anchor the search at anchor - lookback_days, extending
    # forward by lookback_days + lookforward_days. query_news takes a symmetric
    # ±window_days, so we shift the around_date to the window midpoint.
    total_span = lookback_days + lookforward_days
    midpoint = anchor - timedelta(days=(lookback_days - lookforward_days) / 2)
    half_window = (total_span + 1) // 2  # ceil so a span of N+1 gets covered

    from tools.analysis.query_news import execute as run_query
    news_res = await run_query(
        terms=[event_text],
        around_date=midpoint.date().isoformat(),
        window_days=half_window,
        # bigger limit — verify_catalyst is rare, exhaustive matters
        limit=50,
    )
    if not news_res.get("ok"):
        return {"ok": False, "reason": f"news search failed: {news_res.get('reason')}"}

    markers = _date_markers(anchor)
    enriched: list[dict[str, Any]] = []
    for m in news_res["matches"]:
        body = (m.get("snippet") or "") + " " + (m.get("title") or "")
        matched_marker = next((mk for mk in markers if mk.lower() in body.lower()), None)
        enriched.append({
            **m,
            "matched_date_marker": matched_marker,
        })
    # `found` = at least one term-matched post within ±window_days (the SQL
    # already bounds the time window, so a term hit IS a date-anchored hit).
    # The `matched_date_marker` field is supplementary — present when the
    # article explicitly names the calendar date, absent when the article
    # discusses an event as "today" or "yesterday". A FABRICATED catalyst
    # produces 0 term hits in the window; a REAL catalyst produces ≥1.
    found = len(enriched) > 0
    # Confidence tier: 'strong' if any post explicitly names the date,
    # 'window' if only the time-window constraint anchors it.
    confidence = (
        "strong" if any(e.get("matched_date_marker") for e in enriched)
        else ("window" if found else "absent")
    )

    from db import store
    evidence_id = await store.stamp_evidence(
        kind="news_post",
        source_ref_id=(
            f"catalyst:{event_text[:60]}:{anchor.date().isoformat()}"
            f":b{lookback_days}f{lookforward_days}"
        ),
        inputs_json={
            "event_text": event_text,
            "date": anchor.date().isoformat(),
            "lookback_days": lookback_days,
            "lookforward_days": lookforward_days,
            "checked_date_markers": markers,
        },
        outputs_json={
            "found": found,
            "confidence": confidence,
            "n_term_matches": len(enriched),
            "n_date_anchored": sum(1 for e in enriched if e.get("matched_date_marker")),
            "matched_post_ids": [e["post_id"] for e in enriched if e.get("matched_date_marker")] or [e["post_id"] for e in enriched],
        },
        content_snippet=(
            f"catalyst check {event_text!r} @ {anchor.date().isoformat()} "
            f"(-{lookback_days}d / +{lookforward_days}d) → "
            f"{'FOUND' if found else 'NOT FOUND'} ({confidence}, "
            f"{len(enriched)} term hits, "
            f"{sum(1 for e in enriched if e.get('matched_date_marker'))} date-anchored)"
        ),
        computed_by=f"verify_catalyst@{TOOL_VERSION}",
        agent_name=agent_name,
        session_id=session_id,
    )

    return {
        "ok": True,
        "event": event_text,
        "date": anchor.date().isoformat(),
        "lookback_days": lookback_days,
        "lookforward_days": lookforward_days,
        "found": found,
        "confidence": confidence,
        "matches": enriched,
        "checked_date_markers": markers,
        "evidence_id": evidence_id,
    }
