"""One-shot: produce 10 evening slides (one per sector agent) directly,
bypassing the MCP-server tool registry (which hasn't been reloaded yet).

For each agent:
  - read latest agent_state snapshot for headline P&L
  - read top forecasts (by abs(forecast_score)) and active convictions
  - read latest journal entries for "open questions"
  - synthesize sector-appropriate trends/philosophy bullets from sector_map
  - render the 1-page slide via reporting.evening_slide.render_evening_slide
  - print the slide path to stdout

Uses local DB + reporting modules only. Telegram send is a separate step
performed by the caller.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

AGENTS = [
    "atlas", "fab", "fabless", "iron", "maya",
    "rex", "trump", "vera", "volt",
    "energy", "commodity",
]

PHILOSOPHY = {
    "atlas":     ["Macro: rates + FX overlays drive 60% of risk", "Cap any single-asset weight at 0.7", "CASH default 0.4 between regime calls", "Long bonds when DXY weakening + curve steepening"],
    "fab":       ["Equipment cohort sizing capped at 0.6 each (correlated)", "Inverse ETF hedges <=10% notional with momentum confirm", "CASH default 0.85 off-hours", "Cite model_inputs at conviction >=0.5"],
    "fabless":   ["Single-stock semis cap 0.5 (high-vol)", "Bear inverse-ETFs only with momentum break", "CASH 0.7 default; trim into post-NVDA tape", "Mean-revert holds RSI extremes 70/30"],
    "iron":      ["Defense longs run with broader risk-on bid", "No verified inverses for industrials — paper-trail bears", "Sizing skews to top-3 conviction; tail thinly", "Cash 0.4 default; flex into earnings"],
    "maya":      ["Banks drift on rates path; cap 0.5 per name", "Inverse SEF/SKF for sector-bear hedge only", "CASH 0.6 default off-hours", "Lean into oversold mean-revert at RSI<35"],
    "rex":       ["Mega-cap tech: cap each long at 0.7", "Inverse PSQ/SQQQ for cohort hedge", "CASH 0.4 between print weeks", "GGLS/METD/AMZD as single-name bear expressions"],
    "trump":     ["Staples = defensive; cap 0.5 each", "No verified inverses for staples — paper-trail bears", "CASH 0.5 default off-hours", "Lean into post-dividend reset bounces"],
    "vera":      ["Healthcare: cap each long at 0.7 (binary risk)", "Pharma vs biotech mix balances drug-cycle", "CASH 0.4 default", "FDA calendar drives event-driven adds"],
    "volt":      ["Utilities + REITs: rate-sensitive; cap 0.85 leaders", "Inverse REK for REIT bear; SRS for IYR", "CASH 0.5 default", "AI-power-demand floor names sized larger"],
    "energy":    ["Integrated + E&P + services + refiners + midstream", "Inverse DUG/ERY for crude bear; SCO for tactical USO short", "CASH 0.5 default; flex into OPEC + EIA events", "Cycle-aware sizing — cap each name at 0.6"],
    "commodity": ["Precious + base + ag miners + broad commodity ETFs", "GLL for gold bear; ZSL for silver bear (verify before use)", "CASH 0.5 default", "Real-rates + DXY drive precious; China stimulus drives base"],
}


async def build_agent_inputs(agent_name: str) -> dict:
    """Pull the data we need from the DB and shape it into the kwargs for
    render_evening_slide."""
    from db.schema import get_pool
    pool = await get_pool()

    # 1. Latest agent_state snapshot for headline.
    async with pool.acquire() as conn:
        snap = await conn.fetchrow(
            """SELECT realized_pnl::float8 AS r, unrealized_pnl::float8 AS u,
                      total_pnl::float8 AS t, n_positions
               FROM agent_state
               WHERE agent_name=$1
               ORDER BY snapshot_at DESC LIMIT 1""",
            agent_name,
        )
        # 2. Top convictions (by conviction desc).
        convs = await conn.fetch(
            """SELECT symbol, direction, conviction::float8 AS c,
                      expected_return_pct::float8 AS er,
                      time_to_target_days AS ttd, rationale
               FROM agent_conviction
               WHERE agent_name=$1 AND expires_at > NOW() AND conviction > 0
               ORDER BY conviction DESC LIMIT 5""",
            agent_name,
        )
        # 3. Top forecasts (by abs(score) desc).
        fcs = await conn.fetch(
            """SELECT symbol, expected_return_pct::float8 AS er,
                      likelihood::float8 AS lk,
                      time_to_target_days AS ttd,
                      forecast_score::float8 AS sc, method
               FROM agent_forecast
               WHERE agent_name=$1 AND expires_at > NOW()
               ORDER BY abs(forecast_score) DESC LIMIT 6""",
            agent_name,
        )
        # 4. Open thesis predictions (un-resolved).
        opens = await conn.fetch(
            """SELECT title, body, verify_by, status
               FROM agent_thesis
               WHERE agent_name=$1 AND kind='prediction' AND status='open'
                 AND (verify_by IS NULL OR verify_by >= CURRENT_DATE)
               ORDER BY verify_by NULLS LAST, created_at DESC LIMIT 4""",
            agent_name,
        )

    # Headline.
    if snap:
        headline = (
            f"P&L: ${float(snap['t']):+,.0f} cumulative "
            f"(real ${float(snap['r']):+,.0f} / unreal ${float(snap['u']):+,.0f}, "
            f"{int(snap['n_positions'])} positions)"
        )
    else:
        headline = "P&L: no agent_state snapshot available"

    # Theses bullets — top convictions.
    theses = []
    for c in convs:
        rat = (c["rationale"] or "").strip().split(".")[0][:100]
        ttd = f", {c['ttd']}d" if c["ttd"] else ""
        er = f", E[ret]={float(c['er']):+.1f}%" if c["er"] is not None else ""
        theses.append(
            f"{c['direction'].upper()} {c['symbol']} conv={float(c['c']):.2f}{er}{ttd} — {rat}"
        )

    # Trends bullets — top-magnitude forecasts as catalysts/watches.
    trends = []
    for f in fcs:
        method = (f["method"] or "").strip()[:60]
        sign = "↑" if float(f["er"] or 0) >= 0 else "↓"
        trends.append(
            f"{f['symbol']} {sign} E[ret]={float(f['er']):+.1f}% × L={float(f['lk']):.2f} → score={float(f['sc']):+.3f} ({f['ttd']}d) | {method}"
        )

    # Open questions — pending predictions.
    open_qs = []
    for o in opens:
        title = (o["title"] or "").strip().split("\n")[0][:90]
        vb = o["verify_by"]
        open_qs.append(f"verifies {vb}: {title}" if vb else f"open: {title}")
    if not open_qs:
        open_qs = ["No predictions due in the next session", "Watching tape for fresh setups Monday open"]

    return {
        "agent_name": agent_name,
        "headline": headline,
        "trends": trends or ["(no forecasts published this hour)"],
        "theses": theses or ["(no active convictions)"],
        "philosophy": PHILOSOPHY.get(agent_name, ["(no philosophy notes loaded)"]),
        "open_questions": open_qs,
    }


async def main() -> None:
    from reporting.evening_slide import render_evening_slide
    paths: list[tuple[str, str]] = []
    for ag in AGENTS:
        try:
            kw = await build_agent_inputs(ag)
            path = await render_evening_slide(**kw)
            if path is None:
                print(f"{ag}\tFAIL\t(no slide produced)", file=sys.stderr)
                continue
            paths.append((ag, str(path)))
            print(f"{ag}\t{path}")
        except Exception as exc:
            print(f"{ag}\tERROR\t{type(exc).__name__}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
