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
    "rex": "rex_guidance",
    "maya": "maya_guidance",
    "atlas": "atlas_guidance",
    "titan": "titan_guidance",
    "vera": "vera_guidance",
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
            open_theses = await store.get_open_theses(agent_name, limit=10)
            due = await store.get_theses_due(agent_name, on_or_before=today_iso)
            resolved = await store.get_recent_resolutions(agent_name, limit=3)
            due_ids = {t["id"] for t in due}
        except Exception as e:
            open_theses, due, resolved, due_ids = [], [], [], set()

        lines.append("--- YOUR JOURNAL ---")
        if open_theses:
            lines.append("Open theses (most recent first):")
            for t in open_theses:
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

NETTING: Mike's allocator runs net_inverse_pairs after collecting all desk
convictions. If a long-underlying position from one agent and a long-on-its-
inverse from another agent cancel out, the allocator collapses them into a
single net position — you don't need to coordinate with peers. Publish your
view honestly and let the netting layer handle desk-level offsets.

Direct-short submissions (direction='short' on individual stocks) will be
SKIPPED by the allocator — they record paper-trail but generate no orders.
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
