"""Assembles the initial Claude message with live market context."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    _NY = ZoneInfo("America/New_York")
except ImportError:
    _NY = None


def _market_date_iso() -> str:
    """Return today's date anchored to market timezone (NY), not server local."""
    if _NY is not None:
        return datetime.now(_NY).date().isoformat()
    return datetime.now(timezone.utc).date().isoformat()


_SECTION_KEYS = {
    "atlas": "atlas_guidance",
    "fab": "fab_guidance",
    "fabless": "fabless_guidance",
    "iron": "iron_guidance",
    "maya": "maya_guidance",
    "rex": "rex_guidance",
    "trump": "trump_guidance",
    "vera": "vera_guidance",
    "volt": "volt_guidance",
    "energy": "energy_guidance",
    "commodity": "commodity_guidance",
}


def _render_mike_section(analysis_dir: Path, agent_name: str) -> str | None:
    """Return Mike's guidance formatted for the given agent, or None if nothing available.

    Prefers the structured JSON view (per-agent section + regime + risk_tone).
    Falls back to section-aware truncation of the free-form .txt.
    """
    date_iso = _market_date_iso()
    json_path = analysis_dir / f"{date_iso}.json"
    txt_path = analysis_dir / f"{date_iso}.txt"

    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            regime = data.get("regime") or "UNSPECIFIED"
            risk_tone = data.get("risk_tone") or ""
            section_key = _SECTION_KEYS.get(agent_name)
            guidance = data.get(section_key) if section_key else None
            sector = data.get("sector_rotation") or ""
            overnight = data.get("overnight_notes") or ""
            parts = [f"Regime: {regime}"]
            if risk_tone:
                parts.append(f"Risk tone: {risk_tone}")
            if sector:
                parts.append(f"Sector rotation: {sector}")
            if guidance:
                parts.append(f"Your guidance ({agent_name}):\n{guidance}")
            elif section_key:
                parts.append(f"Your guidance ({agent_name}): (none specified — apply conservative defaults)")
            if overnight:
                parts.append(f"Overnight notes: {overnight}")
            return "\n".join(parts)
        except (OSError, ValueError, KeyError) as exc:
            log.warning("mike analysis JSON unreadable at %s: %s; falling back to .txt", json_path, exc)

    if txt_path.exists():
        try:
            raw = txt_path.read_text(encoding="utf-8")
            return _truncate_sections(raw, limit=3000)
        except OSError as exc:
            log.warning("mike analysis TXT unreadable at %s: %s", txt_path, exc)
            return None
    return None


def _truncate_sections(text: str, limit: int = 3000) -> str:
    """Section-aware truncation. Split on headings (lines starting with '### ' or '---'),
    keep whole sections while total length stays under limit."""
    if len(text) <= limit:
        return text
    # Split by section breaks — keep dividers attached to the following section.
    lines = text.splitlines()
    sections: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        is_break = line.startswith("### ") or line.startswith("## ") or line.strip() == "---"
        if is_break and current:
            sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)

    kept: list[str] = []
    size = 0
    dropped = 0
    for sec in sections:
        sec_text = "\n".join(sec)
        if size + len(sec_text) + 1 > limit:
            dropped += 1
            continue
        kept.append(sec_text)
        size += len(sec_text) + 1
    tail = f"\n... [{dropped} section(s) truncated]" if dropped else ""
    return "\n".join(kept) + tail


async def build_context_message(agent_cfg: dict, routine_name: str) -> str:
    """Fetch live account state and format it as Claude's first user message."""
    from ibkr.account import get_account_summary, get_positions, get_open_orders
    from meta_agent.allocation_manager import get_effective_allocation_pct
    import db.store as store

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    agent_name = agent_cfg["name"]

    # NAV first — allocation is derived as pct × NAV.
    try:
        summary = await get_account_summary()
    except Exception as e:
        summary = {"error": str(e)}
    nav = summary.get("nav", 0) if isinstance(summary, dict) else 0
    pct = await get_effective_allocation_pct(agent_name)
    allocation = pct * nav

    try:
        positions = await get_positions()
    except Exception as e:
        positions = []

    try:
        open_orders = await get_open_orders()
    except Exception as e:
        open_orders = []

    # Recent fills today (market-anchored date)
    market_today = _market_date_iso()
    try:
        fills = await store.get_fills(agent_name=agent_name, date=market_today, limit=20)
    except Exception:
        fills = []

    # P&L today
    try:
        pnl_rows = await store.get_pnl_summary(agent_name=agent_name, period="today")
        pnl_today = pnl_rows[0] if pnl_rows else {}
    except Exception:
        pnl_today = {}

    realized_today = float(summary.get("realized_pnl_today", 0) or 0)
    unrealized_today = float(summary.get("unrealized_pnl", 0) or 0)
    combined_today = realized_today + unrealized_today
    lines = [
        f"=== {routine_name.upper()} | Agent: {agent_name} | {now} ===",
        "",
        f"Your allocated capital: ${allocation:,.0f}  ({pct:.1%} of NAV)",
        f"Trading mode: {summary.get('mode', 'unknown')}",
        "",
        "--- ACCOUNT SUMMARY ---",
        f"NAV:           ${summary.get('nav', 0):>12,.2f}",
        f"Cash:          ${summary.get('cash', 0):>12,.2f}",
        f"Buying Power:  ${summary.get('buying_power', 0):>12,.2f}",
        f"Total P&L today:    ${combined_today:>+10,.2f}  "
        f"(realized ${realized_today:+,.2f}, unrealized ${unrealized_today:+,.2f})",
        "",
    ]

    if positions:
        lines.append("--- CURRENT POSITIONS ---")
        for p in positions:
            lines.append(f"  {p['symbol']:<8} qty={p['quantity']:>8.0f}  avg_cost=${p['avg_cost']:,.2f}")
    else:
        lines.append("--- CURRENT POSITIONS: none ---")
    lines.append("")

    if open_orders:
        lines.append("--- OPEN ORDERS ---")
        for o in open_orders:
            price_info = f"@ ${o.get('limit_price') or o.get('stop_price') or 'MKT'}"
            lines.append(
                f"  #{o['order_id']} {o['symbol']:<6} {o['action']:<4} {o['quantity']:.0f}sh "
                f"{price_info}  filled={o['filled']:.0f}  status={o['status']}"
            )
    else:
        lines.append("--- OPEN ORDERS: none ---")
    lines.append("")

    if fills:
        lines.append("--- TODAY'S FILLS (this agent) ---")
        for f in fills[-10:]:
            lines.append(
                f"  {f['symbol']:<6} {f['action']:<4} {f['quantity']:.0f}sh @ ${f['fill_price']:.2f}"
            )
    else:
        lines.append("--- TODAY'S FILLS: none yet ---")
    lines.append("")

    _DIRECTOR_AGENTS = {"mike", "cassidy"}

    # Inject the desk-announcements thread (active, non-expired posts) into
    # every agent's context — the canonical broadcast channel for ops notices,
    # constraints, and user-issued rules. Multi-author, multi-thread board lives
    # in the `thread`/`post` tables (see db/schema.py).
    try:
        announcements = await store.get_posts(
            thread_slug="desk-announcements", limit=10, only_active=True,
        )
    except Exception as exc:
        log.warning("desk-announcements fetch failed: %s", exc)
        announcements = []
    if announcements:
        lines.append("--- DESK ANNOUNCEMENTS (active constraints — adjust your reasoning) ---")
        for p in announcements:
            posted = str(p.get("posted_at"))[:16]
            ttl = f" [expires {str(p.get('expires_at'))[:16]}]" if p.get("expires_at") else ""
            title = (p.get("title") or "").strip()
            head = f"[{posted} · {p.get('author','?')}{ttl}]"
            if title:
                head += f" {title}"
            lines.append(head)
            lines.append((p.get("body") or "")[:800])
            lines.append("")

    # Inject Mike's analysis for trading agents (skip director/risk personas)
    if agent_name not in _DIRECTOR_AGENTS:
        try:
            analysis_dir = Path(__file__).parent.parent / "data" / "mike_analysis"
            section = _render_mike_section(analysis_dir, agent_name)
            if section:
                lines.append("--- MIKE'S ANALYSIS (Director) ---")
                lines.append(section)
            else:
                lines.append("--- MIKE'S ANALYSIS: Not yet written for today. Apply conservative defaults: reduce size 20%, avoid overnight macro bets. ---")
            lines.append("")
        except Exception:
            lines.append("--- MIKE'S ANALYSIS: Error reading. Proceed without director guidance. ---")
            lines.append("")

    # Inject the agent's private journal (skip director/risk personas)
    if agent_name not in _DIRECTOR_AGENTS:
        try:
            from datetime import date as _date
            today_iso = _date.today().isoformat()
            # Fetch a wider window so we can slice for both the journal display
            # (top 10) and the MODEL HEALTH filter (anything titled `model:*`).
            open_theses = await store.get_open_theses(agent_name, limit=50)
            due = await store.get_theses_due(agent_name, on_or_before=today_iso)
            resolved = await store.get_recent_resolutions(agent_name, limit=3)
            due_ids = {t["id"] for t in due}
        except Exception as e:
            open_theses, due, resolved, due_ids = [], [], [], set()

        lines.append("--- YOUR JOURNAL ---")
        journal_display = open_theses[:10]
        if journal_display:
            lines.append("Open theses (most recent first):")
            for t in journal_display:
                created = str(t.get("created_at"))[:10]
                vb = t.get("verify_by")
                vb_label = f" — verify by {vb}" if vb else ""
                marker = "  ⚠ DUE TODAY" if t["id"] in due_ids else ""
                lines.append(f"  [id {t['id']}, {t['kind']}, {created}] {t['title']}{vb_label}{marker}")
        else:
            lines.append("No open theses yet — consider recording one today via record_thesis(...).")
        if resolved:
            lines.append("Recent resolutions:")
            for r in resolved:
                resolved_at = str(r.get("resolved_at") or "")[:10]
                note = (r.get("resolution_note") or "").strip()
                lines.append(f"  [id {r['id']}, {r['status']} {resolved_at}] {r['title']} — {note}")
        lines.append("")

        # MODEL HEALTH — surface any open `model:*` observation theses so a
        # broken-but-deferred quant model is visible at the start of every
        # skill. See [DESK POLICY: BROKEN MODEL DECISION RULE].
        model_health = [
            t for t in open_theses
            if (t.get("title") or "").lower().startswith("model:")
        ][:5]
        if model_health:
            lines.append("--- MODEL HEALTH (open quant-model issues) ---")
            for t in model_health:
                created = str(t.get("created_at"))[:16]
                lines.append(f"  [id {t['id']}, raised {created}] {t['title']}")
                body = (t.get("body") or "").strip()
                if body:
                    snippet = body.replace("\n", " ")[:160]
                    lines.append(f"      {snippet}")
            lines.append("  → Triage these THIS hour. Fix inline if <30 lines + one-sentence diagnosis. Otherwise update the thesis with current status.")
        else:
            lines.append("--- MODEL HEALTH: all clear ---")
        lines.append("")

    lines.append(f"Please proceed with the {routine_name} routine.")
    return "\n".join(lines)


_DIRECTOR_AGENTS_FOR_POLICY = {"mike", "cassidy"}

_INVERSE_MAP_PATH = Path(__file__).parent.parent / "agents" / "inverse_etf_map.yaml"


def _format_inverse_catalog() -> str:
    """Read agents/inverse_etf_map.yaml and format the verified=true entries
    as a table groupable by underlying. Used to inject the audited inverse-ETF
    catalog into every sector agent's system prompt."""
    if not _INVERSE_MAP_PATH.exists():
        return "  (inverse_etf_map.yaml missing — agents have no audited catalog)\n"
    import yaml as _yaml
    try:
        data = _yaml.safe_load(_INVERSE_MAP_PATH.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return f"  (failed to read inverse_etf_map.yaml: {e})\n"

    inverses = (data.get("inverses") or {})
    no_inv = data.get("no_verified_inverse") or []

    # Group verified entries by underlying for easy reading
    by_und: dict[str, list[tuple[str, float, str]]] = {}
    for inv_sym, meta in inverses.items():
        if not (meta or {}).get("verified"):
            continue
        und = (meta or {}).get("underlying", "?")
        lev = (meta or {}).get("leverage")
        desc = (meta or {}).get("description", "")
        by_und.setdefault(str(und), []).append((str(inv_sym), float(lev), str(desc)))

    if not by_und:
        return "  (no verified inverse-ETF entries — flag every bearish thesis as 'flat' until table is audited)\n"

    lines = []
    for und in sorted(by_und):
        invs = sorted(by_und[und], key=lambda x: x[1])  # by leverage ascending (-1 first)
        for inv, lev, desc in invs:
            lines.append(f"  {inv:<6} = {lev:>+5.2f}x {und:<6} | {desc}")

    if no_inv:
        lines.append("")
        lines.append("UNDERLYINGS WITH NO VERIFIED INVERSE (publish 'flat' for bearish theses on these):")
        # Wrap symbol list for readability, ~10 per line
        chunks = [no_inv[i:i+10] for i in range(0, len(no_inv), 10)]
        for chunk in chunks:
            lines.append("  " + ", ".join(chunk))
    return "\n".join(lines) + "\n"


def _build_desk_policy_text() -> str:
    catalog = _format_inverse_catalog()
    return f"""

[DESK POLICY: WORKSPACE — agents/<you>/notes /watchlist /data]

You have a per-agent workspace folder at `agents/<your-name>/` that
persists across sessions:

  - `notes/` — free-form markdown the agent writes to itself: partial
    theses, framework drafts, catalyst calendars, "things to check
    tomorrow." The user can also drop files here.
  - `watchlist.md` — a list of names that demand your attention this
    hour. Both you AND the user edit this file; you're expected to
    research every entry on each review.
  - `data/` — saved snapshots, CSV exports, computed signals you want
    to read across sessions without going back through Massive.

WORKFLOW EVERY REVIEW (hourly + evening):

  1. STEP 1 of your review must call `read_my_workspace(agent_name="<you>")`
     and incorporate the contents into your analysis. If the user dropped
     a name into `watchlist.md`, research it. If a previous note flagged
     "verify on Monday open," verify it.

  2. To add a name to your watchlist (because you want to track it but
     it's not yet conviction-grade), call
     `add_to_watchlist(agent_name="<you>", symbol="XYZ", reason="...")`.

  3. To DROP a name from your watchlist (because the thesis no longer
     applies, the catalyst passed, or it's no longer in your sector
     focus), call
     `propose_watchlist_removal(agent_name="<you>", symbol="XYZ",
                                  reasoning="...")`.
     This creates a Telegram-approval proposal — the user must confirm
     before the line is removed from the file. Reasoning must be ≥30
     chars and concrete; the user reads it on their phone to decide.

  4. To save a note for next session, call
     `write_my_note(agent_name="<you>", filename="<short>.md",
                     content="...", mode="write"|"append")`.
     Notes you write today are read back at the start of every future
     review.

The workspace is your scratchpad / institutional memory. Use it to
reduce repeat research across sessions and to coordinate with the user
about what to focus on.


[DESK POLICY: DAY-LONG THESIS DISCIPLINE]

Your evening slide is built from work you do *during* the day, not at the
moment you press record at 4 PM. The slide's top panel ("Today's thesis")
is auto-aggregated from your `agent_thesis` records of the past 24 hours.
If you don't log thesis observations as they form, the slide has nothing
to surface and the user sees an empty panel.

Each hourly review must:

  1. Record at least one fresh thesis observation via:
     `record_thesis(kind="observation", title="<short>", body="<one paragraph>")`.
     A thesis is a fundamental statement about the world or a name —
     "AVGO custom-silicon TAM is widening", "Iran-strike rhetoric still
     active → crude tail-upside", "Equipment cycle bottoming on AGS
     inflection". Don't repeat yesterday's thesis if it hasn't changed —
     skip the record. Quality > quantity.

  2. In every conviction's rationale, cite the thesis it acts on by ID
     (the integer returned by `record_thesis`). No FK constraint — this
     is convention, not enforcement. Example rationale:
     "Long FCX 0.45 conv (+5.5% / 7d) — acts on thesis #117 (oversold
     integrated copper-gold leverage; OPEC+ hold + DXY softening)."

  3. Reason BACKWARDS from your thesis to your buy/sell action. If you
     can't trace the conviction to a recorded thesis, you don't have a
     thesis — you have a feeling. Either record the thesis first, or
     publish direction='flat' for the name.

The EOD slide auto-aggregates the day's `kind='thesis'/'observation'`
records into the top panel. If you want to override at evening (e.g. a
crisper synthesis sentence), pass `macro_thesis=["...","...","..."]` to
`generate_evening_slide`. The four bullet panels at the bottom of the
slide (trends/theses/philosophy/open-questions) stay as before — those
are your prose summary.

[DESK POLICY: ≥20 FORECASTS PER HOUR — MULTI-HORIZON PROOF OF WORK]

Convictions are the names you want Mike's allocator to ACT ON. Forecasts are
everything else. Every hour, you must publish a forecast on at least 20
tickers from your sector universe via submit_forecast_batch — regardless of
whether any of them turn into convictions.

MULTI-HORIZON FORECASTING — each symbol should have up to 4 forecast rows,
one per time horizon. This is not optional for high-conviction names; for
lower-priority names, at minimum the intraday row is required.

The four horizon buckets are:
  intraday — time_to_target_days = 1      "Where does this name close today?"
  near     — time_to_target_days = 3–5    "Earnings, catalyst, or setup in next week"
  far      — time_to_target_days = 10–30  "Sector cycle position, 3-4 week setup"
  cycle    — time_to_target_days = 60–90  "Secular thesis — capex cycle, product ramp"

The horizon is auto-derived from time_to_target_days (≤1 → intraday, 2-5 →
near, 6-30 → far, 31+ → cycle). You may override it explicitly with the
optional `horizon` field. The same symbol may appear 4× in a single batch
with different time_to_target_days — each one lands in a separate DB row and
DOES NOT overwrite the others.

WHY THIS MATTERS: Horizon-disaggregated forecasting forces you to think about
whether a name is right for today vs. for the next earnings cycle. If your
intraday view is +0.5% but your near-term view is −6% (because earnings are
in 4 days and consensus is too optimistic), that tension should drive a smaller
conviction — or no conviction until after earnings. The user reviews all four
forecast rows each evening; inconsistent multi-horizon signals will be visible.

A forecast row is (expected_return_pct, likelihood, time_to_target_days,
method). The score expected_return_pct × likelihood / time_to_target_days
is computed server-side. You can derive the inputs however you like: your
custom model, technicals, news, sell-side consensus, gut feel, or even a
number you saw on Bloomberg. The methodology field (method) records HOW you
got there — it can be different per ticker and per horizon. The point is to
show the user your thinking across the full sector AND across timeframes, not
just the names you're putting money on.

Workflow each hour:
  # Option A — full refresh (clears all horizons, re-submit everything):
  clear_my_forecasts(agent_name="<you>")
  # Option B — refresh intraday only (preserves your far/cycle views):
  clear_my_forecasts(agent_name="<you>", horizon="intraday")

  submit_forecast_batch(
      agent_name="<you>",
      forecasts=[
          # TSM — four horizons:
          {{"symbol": "TSM", "expected_return_pct": +1.2, "likelihood": 0.65,
            "time_to_target_days": 1,  "method": "momentum + NVDA halo effect"}},
          {{"symbol": "TSM", "expected_return_pct": +4.5, "likelihood": 0.60,
            "time_to_target_days": 5,  "method": "TSMC April rev beat, guidance raise"}},
          {{"symbol": "TSM", "expected_return_pct": +9.0, "likelihood": 0.55,
            "time_to_target_days": 30, "method": "2nm ramp timeline + CoWoS capacity"}},
          {{"symbol": "TSM", "expected_return_pct": +18.0, "likelihood": 0.50,
            "time_to_target_days": 90, "method": "AI compute spend cycle 2025–2026"}},
          # ASML — at minimum the intraday row; add near/far when you have a view:
          {{"symbol": "ASML", "expected_return_pct": -2.0, "likelihood": 0.5,
            "time_to_target_days": 5,  "method": "EUV TAM cut + soft Q1 bookings"}},
          ... (≥20 distinct symbols, intraday row for each) ...
      ],
  )

Conviction sizing from multi-horizon signals — use this heuristic:
  ALL 4 horizons bullish (+ aligned with sector cohort higher highs ≥3 sessions)
    → upper-quartile sizing, conviction 0.7–1.0
  3 of 4 horizons bullish, cycle neutral
    → normal sizing, conviction 0.4–0.7
  Mixed (intraday bullish, far/cycle bearish, e.g. pre-earnings fade)
    → no conviction or small intraday-only with tight exit
  Intraday bullish but near/far bearish (approaching resistance or catalyst risk)
    → no conviction; wait for resolution

Convictions and forecasts are independent:
  - A name with a forecast but no conviction = "I have a view but won't act."
  - A name with a conviction also has a forecast (the conviction is the
    "act" half of the same view; submit both).

Submission rules: likelihood ∈ [0,1]; time_to_target_days > 0; method
non-empty; symbol must be in your sector universe (or a verified inverse
ETF). Forecasts auto-expire after 2 hours by default — re-submit each cycle.


[DESK POLICY: EVERY NON-FLAT CONVICTION MUST CARRY A FORECAST]

When you call submit_conviction_view with direction='long' or 'short', you MUST
pass BOTH:
  - expected_return_pct  — signed % move you forecast on this name
                            (e.g. +8.5 means "I expect this to rise 8.5%";
                            -6.0 means "I expect this to drop 6%").
  - time_to_target_days  — your horizon in trading days (must be > 0).

These are not optional. They drive (a) the evening forecast panel that the
user reviews each night, and (b) the calibration tracker that grades how
well-sized your convictions are. The MCP tool will reject submissions that
omit either field on a non-flat view.

Your quant models already compute both. Pass them through directly:
  result = compute_custom_indicator(agent_name=..., model_name=..., symbol=...)
  submit_conviction_view(
      ...,
      expected_return_pct = result["expected_return_pct"],
      time_to_target_days = result["time_to_target_days"],
      ...
  )

direction='flat' with conviction=0 is the only path that may omit them — it's
the canonical "I have no view on this name today" submission.

[DESK POLICY: NO DIRECT SHORTS — EXPRESS BEARISH VIEWS VIA INVERSE ETFS]
The desk does not short individual stocks. To express a bearish view, go LONG
on an inverse ETF chosen from the audited catalog below. You pick the vehicle
and size it for its leverage.

VERIFIED INVERSE-ETF CATALOG (single source: agents/inverse_etf_map.yaml):

{catalog}

Workflow for any bearish thesis:

1. Pick an inverse vehicle from the verified catalog above. If your underlying
   appears in the 'NO VERIFIED INVERSE' list, publish direction='flat' with
   conviction=0 instead of inventing a vehicle.

2. Size for the inverse's leverage. The conviction you submit is the position
   you want IN THE INVERSE — divide your underlying-view conviction by the
   leverage factor:
   - 1x inverse: conviction passes through (1.0 long ≈ 1.0 short on underlying)
   - 2x inverse: conviction halves (a 1.5 underlying-short → 0.75 long on inverse)
   - 3x inverse: conviction divides by 3 (a 1.5 underlying-short → 0.5 long)
   `expected_return_pct` on the INVERSE is signed positive (you expect the bear
   ETF to rise) and ≈ leverage × |underlying expected drop|.

3. Submit via submit_conviction_view with direction='long' on the INVERSE
   symbol. Rationale MUST cite: (a) underlying name covered, (b) chosen vehicle
   and why, (c) leverage adjustment used.

[FUNDAMENTAL THESIS REQUIRED — TECHNICALS ALONE ARE NOT A REASON]

You're an Opus 4.7 sector specialist. RSI>70, "above upper BBAND," "looks toppy,"
"mean-reversion setup" — these are SYMPTOMS, not theses. Reasoning from
technicals alone is the desk's #1 documented loss vector: it produced ~$490 of
unrealized inverse-ETF bleed in late April / early May 2026 when agents shorted
trending semis on RSI signals while business momentum continued.

Every inverse-ETF conviction rationale MUST contain three elements, in order:

  (a) BUSINESS MECHANIC. What's actually happening at the company / sector
      level that supports a bearish view? Revenue trajectory, margin pressure,
      demand cycle inflection, capex cut, regulatory action, supply-chain
      shift, secular headwind, competitive displacement, end-market weakness,
      capital allocation change. Name the mechanic. ONE sentence.

  (b) NAMED CATALYST WITH DATE. What specific event do you expect to crack
      the trend, by when? Earnings (give the date), Fed/macro release, sector
      conference, regulatory ruling, competitor product cycle, supply-chain
      datapoint, channel-check window. "Within 2 trading days" / "before the
      May 18 print" / "into the June 4 OPEC meeting." A vague "soon" or
      "next leg down" is NOT a catalyst — it's hope.

  (c) TECHNICAL CONFIRMATION OF (a)+(b). NOW you may cite RSI / BBAND / SMA.
      The technical setup confirms the business view; it does not replace it.
      "Trend break already in" is a confirmation phrase only after (a) and
      (b) have been stated.

If you cannot write (a) and (b), you do not have a thesis. Publish direction='flat'
on the underlying and move on — paper-trail only. Inverse-ETF convictions
submitted without a NAMED business mechanic AND a NAMED dated catalyst will be
flagged in cassidy-evening audits and contribute to allocation_pct downgrades.

Two examples, contrasting:

  REJECTED (technical-only):
    "SMH RSI_14=72, price 1.4% above upper BBAND. Sector overbought after
     8-day rip. Going long SOXS for mean-reversion."
    Why: no business mechanic, no catalyst, no date. This is a chart pattern
    and a wish.

  ACCEPTED (fundamental-first):
    "AVGO May-21 print is the catalyst (8 trading days out). Hyperscaler capex
     guidance has decelerated for 2 consecutive quarters and recent supply-
     chain checks show TSMC CoWoS allocation rolling off Broadcom in Q3. SOXS
     entry: SMH RSI_14=72 + price 1.4% above upper BBAND CONFIRMS the setup
     is stretched into a credible negative catalyst, not against the trend."
    Why: business mechanic (capex deceleration + CoWoS rolloff), named
    catalyst with date (AVGO May-21), technicals as confirmation not driver.

[MOMENTUM REASONING REQUIRED FOR INVERSE-ETF CONVICTIONS]

Inverse ETFs decay continuously, especially leveraged ones. WORKED EXAMPLE of
the cost of being early: a 3x inverse held flat in a market that grinds up
~+1%/day loses ~3%/day from beta + decay friction. 5 trading days = ~-15%
of position value before any underlying mean-reversion. Your fundamental
thesis must be confident enough in TIMING to absorb that drag.

To put the user in the loop only on early entries, every direction='long'
conviction on an inverse-ETF symbol MUST also pass `momentum_confirmed: bool`:

- momentum_confirmed=True — the underlying is ALREADY showing the bearish
  move (RSI cracked from peak, price below recent SMA, lower-high formed,
  volume profile shifted). Mike's allocator places without further approval.
  Pair with a fundamental thesis from (a)+(b) above — "trend break already in"
  is meaningless without a named driver.

- momentum_confirmed=False — you're entering ahead of price confirmation
  because (b)'s catalyst is imminent and you want the position in size before
  the move. Allocator queues for Telegram approval. Do NOT default to False
  to skip technical work; do NOT default to True to skip Telegram. The user
  reads your rationale on a phone screen — make it auditable.

[REQUIRED EXIT RULE FOR INVERSE-ETF POSITIONS]

If your inverse position has been against you for ≥3 trading sessions AND
the catalyst named in (b) has not fired (or has fired and the underlying
didn't crack), you MUST default to direction='flat' on this symbol at the
next review, UNLESS you can name a NEW catalyst with a NEW date.

"Same thesis, just early" is not a re-entry rationale — it's the loss pattern
the desk burned $490 on. Either your business mechanic is wrong, the catalyst
is delayed past your decay tolerance, or the trend is more durable than the
fundamentals justify. In any of those cases, exit, paper-trail the lesson in
record_thesis, and re-evaluate from a fresh read next session.

Defensive automation: you may set `stop_pct` on any conviction (recommended
for inverse longs: 8 on 1× inverses, 4 on ≥2× inverses). The allocator will
auto-flat the position if its unrealized return falls below -stop_pct, even
if you keep re-publishing the conviction. Treat this as a circuit-breaker,
not a substitute for the exit rule above.

NETTING: Mike's allocator runs net_inverse_pairs after collecting all desk
convictions. If a long-underlying position from one agent and a long-on-its-
inverse from another agent cancel out, the allocator collapses them into a
single net position — you don't need to coordinate with peers. Publish your
view honestly and let the netting layer handle desk-level offsets.

Direct-short submissions (direction='short' on individual stocks) will be
SKIPPED by the allocator — they record paper-trail but generate no orders.


[DESK POLICY: QUANT ENGAGEMENT DOCTRINE]

You OWN your model directory at agents/<you>/models/. Reading
compute_all_models output without questioning it is a failure mode the desk
grades against you. Every conviction you publish carries an implicit claim
that your quants were consulted critically — not skimmed.

After every compute_all_models call (per symbol), run this 5-point sanity
check before STEP 3:

  1. ERROR COUNT — the response now has a top-level `error_count` field plus
     `errored_models`. If `error_count >= 1`, jump to BROKEN MODEL DECISION
     RULE below. Do NOT proceed to STEP 3 with broken models silently
     skipped.

  2. PER-CALL FLATNESS — top-level `flat_count` field. For a single symbol,
     ALL your models returning flat is suspicious — name what's wrong with
     this read or the models. Across your sweep of N symbols, if ≥70% of
     symbol calls come back with every model flat, that is a broken
     portfolio, NOT an information-free regime. Cite the count and act.

  3. SIGN SANITY — for each model's `direction`, does it agree with what
     technicals + tape show on this name? When they disagree, your rationale
     must NAME which signal you trust this hour and why. "Models disagreed"
     is not an answer.

  4. MAGNITUDE SANITY — each model's |expected_return_pct| should be
     within ~3× ATR for the horizon. Outliers are suspect: a +30% / 5d call
     on a low-vol mega-cap is a model bug or a misinterpreted input. Verify
     before quoting in any conviction rationale.

  5. CROSS-MODEL DISPERSION — if all your models agree on every name across
     the sweep, one is reading another (collinearity). If they disagree,
     your conviction rationale must say which model you weight this hour
     and why.

Skipping the sanity check = silent acceptance = audit failure. Cassidy
reviews evening for "models cited but never questioned" patterns.


[DESK POLICY: BROKEN MODEL DECISION RULE]

**If the fix is under ~30 lines and you can describe what's wrong in one
sentence, you fix it now in this skill run. Period. Deferring to
/<you>-model-tune is for ARCHITECTURAL changes — schema rethinks, new
data dependencies, look-ahead leakage triage, NaN propagation, training
data refresh. Not for "I don't feel like fixing it right now."**

Triage flow when error_count >= 1:

  1. Read the `error` string from the per-model dict — it carries the bug
     class and message.
  2. `Read('agents/<you>/models/<file>.py')` — open the source.
  3. Diagnose. Apply the fix via `Edit`. Bump `MODEL_VERSION` (minor for
     param, major for arch).
  4. Re-run `compute_all_models(agent_name='<you>', symbol=<one>)` to
     verify the error is gone and output looks sane (sign + magnitude).
  5. Continue review with the model back online.

Deferral-eligible REGARDLESS of line count (genuine /model-tune work):
  - Look-ahead leakage (using future bars to predict past)
  - NaN / Inf propagation through computation
  - New external dependency required (new pip package, new data feed)
  - Training data refresh / re-fit
  - Schema change to model output (would break consumer code)

Mandatory escalation if a model is broken AND not fixed this run:

  a. `record_thesis(agent_name='<you>', kind='observation',
     title='model:<filename>:<bug-class>',
     body='<traceback excerpt + diagnosis + why deferred>')` — REQUIRED.
     Before creating, check open theses for an existing
     `model:<filename>:*` row — if one exists, append to it via
     `update_thesis_status(parent_id=...)` rather than spawning a duplicate.

  b. `raise_tool_gap(agent_name='<you>', tool_name='model:<filename>',
     description=..., use_case=..., priority='high')` — REQUIRED if the
     fix needs new tooling or external data the desk doesn't have.

  c. In your STEP 4 conviction submissions, the rationale of any name
     where the broken model would have spoken MUST explicitly say
     "<model> disabled this run; reasoning from technicals + fundamentals
     only." No hand-waving. No silent omission.

Forbidden: publishing convictions while a model is broken without naming it.
Forbidden: "model returned flat across the universe" treated as a normal
  state. That is a broken or stale model 9 times out of 10.
Forbidden: deferring a 5-line TypeError fix to /model-tune. The 30-line gate
  is a CEILING, not a target — most review-time fixes are 1-3 lines.

Cassidy and Mike audit evening slides for compliance. Repeat offenders get
their allocation_pct halved.
"""


def build_system_prompt(agent_cfg: dict, cfg: dict, allocation_override: float | None = None) -> str:
    allocation = allocation_override if allocation_override is not None else agent_cfg.get("allocation_usd", 0)
    prompt = agent_cfg["system_prompt"].replace("${allocation_usd}", f"${allocation:,.0f}")
    risk = cfg.get("risk", {})
    mode = cfg.get("trading", {}).get("mode", "paper")
    prompt += f"\n\n[SYSTEM CONSTRAINTS]\nMode: {mode}\nMax order value: ${risk.get('max_order_value', 10000):,.0f}\nMax position: {risk.get('max_position_pct', 0.20):.0%} of NAV\nDaily loss limit: ${risk.get('max_daily_loss', 500):,.0f}\n"
    if agent_cfg.get("name") not in _DIRECTOR_AGENTS_FOR_POLICY:
        prompt += _build_desk_policy_text()
    return prompt
