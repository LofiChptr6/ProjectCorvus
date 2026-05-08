"""1-page evening slide composer.

Bundle each sector agent's evening report into a single PNG that looks like
a slide so the user gets ONE Telegram message per agent instead of
multi-chart + multi-caption traffic. The slide carries:

  - Header banner (agent name, date, headline P&L)
  - Top half:
      • left: 30-day combined P&L chart (`reporting/agent_chart.py` output)
      • right: top-N forecast panel (`reporting/forecast_panel.py` output)
  - Bottom half: 4 bullet panels —
      • Trends / news / catalysts
      • Theses (active convictions / framework calls)
      • Trading philosophy this hour (style notes, sizing rules in play)
      • Open questions / waiting on (calendar events, data gaps)

Assembly is image-based: we re-use the existing PNG renderers (so changes
to those automatically flow through) and lay them onto a single composite
canvas via matplotlib's `imshow`. No re-implementation of the chart logic.

Output: data/charts/slide_{agent}_{YYYYMMDD_HHMMSS}.png
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_OUT_DIR = Path("data/charts")
_NYSE_TZ = ZoneInfo("America/New_York")


def _wrap_bullets(items: list[str], width: int) -> list[str]:
    """Word-wrap each bullet to ``width`` chars; preserve a leading "• " on
    the first wrapped line of each item, indent continuation lines.

    Also escapes any literal `$` so matplotlib's mathtext parser doesn't
    try to render `$5,000` as math mode."""
    if not items:
        return ["(none)"]
    out: list[str] = []
    for raw in items:
        text = (raw or "").strip().lstrip("-•*").strip()
        if not text:
            continue
        # Escape `$` and `_` (the two chars that trip mathtext most often)
        text = text.replace("$", r"\$")
        wrapped = textwrap.wrap(text, width=width) or [""]
        out.append("• " + wrapped[0])
        for cont in wrapped[1:]:
            out.append("  " + cont)
    return out or ["(none)"]


async def render_evening_slide(
    agent_name: str,
    *,
    headline: str,
    macro_thesis: Optional[list[str]] = None,
    trends: Optional[list[str]] = None,
    theses: Optional[list[str]] = None,
    philosophy: Optional[list[str]] = None,
    open_questions: Optional[list[str]] = None,
) -> Optional[Path]:
    """Render the agent's evening slide. Returns the saved PNG path, or None
    if neither component chart could be produced.

    `macro_thesis` is the new top-of-page panel — 2-3 prose bullets in human
    language describing what's happening in the world / sector / agent's
    fundamental view. When None, auto-aggregates from agent_thesis records
    of the past 24 hours."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.image as mpimg
    from matplotlib.gridspec import GridSpec

    # Render component PNGs by reusing the existing tools/modules.
    pnl_path = await _produce_pnl_chart(agent_name)
    fc_path = await _produce_forecast_panel(agent_name)

    if pnl_path is None and fc_path is None:
        log.warning("evening_slide: both charts missing for %s", agent_name)
        return None

    # Macro thesis: agent override OR auto-aggregate from today's agent_thesis.
    if macro_thesis is None:
        macro_thesis = await _auto_macro_thesis(agent_name)

    today_et = datetime.now(_NYSE_TZ)
    # 22×17 canvas — large enough that chart cells stay legible on a
    # phone, small enough that header/thesis/bullet text isn't dwarfed.
    # constrained_layout handles overlap-free packing automatically.
    fig = plt.figure(figsize=(22, 17), facecolor="white",
                     constrained_layout=True)

    # 7 rows × 4 cols. Layout reshape (2026-05-04): macro thesis now
    # lives in the LEFT column above the P&L chart instead of spanning
    # the full top width — the forecast panel on the right takes one
    # extra row of vertical space (rows 1–5 instead of 2–5).
    #   row 0     — header banner (full width)
    #   row 1, cols 0:2 — Today's thesis (left column only)
    #   row 1, cols 2:4 — Forecast panel TOP slice
    #   rows 2-5, cols 0:2 — Combined P&L
    #   rows 2-5, cols 2:4 — Forecast panel CONTINUES
    #   row 6     — 4 bullet panels (full width)
    gs = GridSpec(
        nrows=7, ncols=4,
        height_ratios=[0.40, 0.85, 2.0, 2.0, 2.0, 2.0, 1.0],
        figure=fig,
    )

    # ── Header banner ────────────────────────────────────────────────────
    ax_header = fig.add_subplot(gs[0, :])
    ax_header.set_axis_off()
    ax_header.text(
        0.0, 0.65, agent_name.upper(),
        fontsize=40, fontweight="bold", va="center", ha="left",
        color="#0d2347", transform=ax_header.transAxes,
    )
    ax_header.text(
        1.0, 0.65, today_et.strftime("%Y-%m-%d %H:%M ET"),
        fontsize=20, va="center", ha="right", color="#666",
        transform=ax_header.transAxes,
    )
    ax_header.text(
        0.0, 0.05, (headline or "").replace("$", r"\$"),
        fontsize=20, va="bottom", ha="left", color="#333",
        transform=ax_header.transAxes,
    )
    ax_header.plot([0.0, 1.0], [0.0, 0.0], color="#0d2347", lw=1.2,
                   transform=ax_header.transAxes, clip_on=False)

    # ── Fundamental thesis panel (left column above P&L) ────────────────
    ax_thesis = fig.add_subplot(gs[1, 0:2])
    ax_thesis.set_axis_off()
    ax_thesis.text(
        0.0, 1.0, "Today's thesis",
        fontsize=18, fontweight="bold", va="top", ha="left", color="#0d2347",
        transform=ax_thesis.transAxes,
    )
    thesis_lines = _wrap_bullets(macro_thesis or [], width=80)
    if thesis_lines == ["(none)"]:
        thesis_lines = [
            "• (no thesis recorded today — agent should `record_thesis` "
            "during the day to populate this panel)"
        ]
    body = "\n".join(thesis_lines[:10])
    ax_thesis.text(
        0.0, 0.82, body,
        fontsize=14, color="#222", va="top", ha="left", linespacing=1.4,
        transform=ax_thesis.transAxes, family="DejaVu Sans",
    )
    # Subtle bottom rule
    ax_thesis.plot([0.0, 1.0], [-0.05, -0.05], color="#cccccc", lw=0.8,
                   transform=ax_thesis.transAxes, clip_on=False)

    # ── Charts: P&L (left half, rows 2–5) | forecast panel (right half, rows 1–5)
    # Forecast spans an extra row (1–5 vs 2–5) because the thesis vacated
    # the right side; gives the panel 5/4 more vertical space.
    ax_pnl = fig.add_subplot(gs[2:6, 0:2])
    ax_pnl.set_axis_off()
    if pnl_path is not None and pnl_path.exists():
        try:
            img = mpimg.imread(str(pnl_path))
            # Default imshow aspect preserves source proportions — the
            # alternative ("auto") stretches the embedded chart and
            # distorts internal text. Whitespace is OK; legibility isn't.
            ax_pnl.imshow(img)
            ax_pnl.set_title("Combined P&L (3d, hourly · trading hours only)",
                             fontsize=17, loc="left",
                             color="#0d2347", pad=6, fontweight="semibold")
        except Exception as exc:
            log.warning("evening_slide: failed to embed pnl chart %s: %s", pnl_path, exc)
            ax_pnl.text(0.5, 0.5, "(P&L chart unavailable)",
                        ha="center", va="center", color="#888", fontsize=13,
                        transform=ax_pnl.transAxes)
    else:
        ax_pnl.text(0.5, 0.5, "(P&L chart unavailable)",
                    ha="center", va="center", color="#888", fontsize=13,
                    transform=ax_pnl.transAxes)

    ax_fc = fig.add_subplot(gs[1:6, 2:4])
    ax_fc.set_axis_off()
    if fc_path is not None and fc_path.exists():
        try:
            img = mpimg.imread(str(fc_path))
            ax_fc.imshow(img)
            ax_fc.set_title("Forecast panel — top tickers (price + agent's indicators + dashed forecast)",
                            fontsize=17, loc="left", color="#0d2347",
                            pad=6, fontweight="semibold")
        except Exception as exc:
            log.warning("evening_slide: failed to embed forecast panel %s: %s", fc_path, exc)
            ax_fc.text(0.5, 0.5, "(forecast panel unavailable)",
                       ha="center", va="center", color="#888", fontsize=13,
                       transform=ax_fc.transAxes)
    else:
        ax_fc.text(0.5, 0.5, "(forecast panel unavailable)",
                   ha="center", va="center", color="#888", fontsize=13,
                   transform=ax_fc.transAxes)

    # ── Bullet panels: 4 columns, single row ─────────────────────────────
    panels = [
        ("Trends / catalysts",   trends,         "#1565c0"),
        ("Theses",               theses,         "#2e7d32"),
        ("Trading philosophy",   philosophy,     "#6a1b9a"),
        ("Open questions / waiting on", open_questions, "#c62828"),
    ]
    for col, (title, items, color) in enumerate(panels):
        ax_b = fig.add_subplot(gs[6, col])
        ax_b.set_axis_off()
        ax_b.text(0.0, 1.0, title, fontsize=16, fontweight="bold",
                  color=color, va="top", ha="left", transform=ax_b.transAxes)
        wrapped = _wrap_bullets(items or [], width=44)
        body = "\n".join(wrapped[:14])  # cap 14 lines per panel
        ax_b.text(0.0, 0.92, body, fontsize=12, color="#222",
                  va="top", ha="left", linespacing=1.35,
                  transform=ax_b.transAxes,
                  family="DejaVu Sans")
        # Faint separator on the left edge (except col 0)
        if col > 0:
            ax_b.plot([-0.02, -0.02], [0.0, 1.0], color="#ddd", lw=0.8,
                      transform=ax_b.transAxes, clip_on=False)

    fig.suptitle("", y=1.0)  # silence default
    # constrained_layout (set at figure construction above) handles the
    # spacing automatically without the tight_layout / imshow conflict.

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = _OUT_DIR / f"slide_{agent_name}_{today_et.strftime('%Y%m%d_%H%M%S')}.png"
    # Save at full figsize (28x22 @ 200dpi = 5600x4400 px). Skip
    # bbox_inches="tight" because it interacts with imshow axes and
    # collapses the figure to the inner-content bounds.
    fig.savefig(out, dpi=200, facecolor="white")
    plt.close(fig)
    return out


# ── Component chart producers ────────────────────────────────────────────────

async def _produce_pnl_chart(agent_name: str) -> Optional[Path]:
    """Use reporting.pnl_curve (hourly, trading-hours-compressed, BUY/SELL
    fill markers) for the slide's left-side P&L chart. Falls back to None
    if the agent has no agent_state snapshots in the window."""
    from reporting.pnl_curve import render_agent_curve
    try:
        path = await render_agent_curve(agent_name, since="3d")
        return path if path and Path(path).exists() else None
    except Exception as exc:
        log.warning("evening_slide: pnl_curve failed for %s: %s", agent_name, exc)
        return None


async def _produce_forecast_panel(agent_name: str) -> Optional[Path]:
    from reporting.forecast_panel import render_forecast_panel
    try:
        return await render_forecast_panel(agent_name)
    except Exception as exc:
        log.warning("evening_slide: forecast_panel failed for %s: %s",
                    agent_name, exc)
        return None


async def _auto_macro_thesis(agent_name: str) -> list[str]:
    """Pull the agent's recent thesis observations and turn them into 3
    prose bullets for the top of the slide. Dedup on title, take the most
    recent entries within the past 24h that look like macro/sector reads
    rather than pending predictions."""
    try:
        from db import store
        rows = await store.get_recent_theses(
            agent_name, hours=24,
            kinds=("thesis", "observation"), limit=8,
        )
    except Exception as exc:
        log.warning("evening_slide: get_recent_theses failed: %s", exc)
        return []

    bullets: list[str] = []
    seen_titles: set[str] = set()
    for r in rows:
        title = (r.get("title") or "").strip()
        body = (r.get("body") or "").strip()
        if not title:
            continue
        key = title.lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        # Compact: "title — first sentence of body". Cap to ~140 chars.
        first_sentence = body.split("\n")[0].split(". ")[0].strip()
        if first_sentence and first_sentence != title:
            text = f"{title} — {first_sentence}"
        else:
            text = title
        bullets.append(text[:200])
        if len(bullets) >= 3:
            break
    return bullets


# ── CLI ──────────────────────────────────────────────────────────────────────

def _main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--agent", required=True)
    p.add_argument("--headline", default="P&L: $? today (no data)")
    p.add_argument("--macro-thesis", nargs="*", default=None,
                   help="3 prose bullets for the top thesis panel; "
                        "if omitted, auto-aggregates from agent_thesis.")
    p.add_argument("--trends", nargs="*", default=[])
    p.add_argument("--theses", nargs="*", default=[])
    p.add_argument("--philosophy", nargs="*", default=[])
    p.add_argument("--open-questions", nargs="*", default=[])
    args = p.parse_args()

    async def run() -> None:
        path = await render_evening_slide(
            args.agent,
            headline=args.headline,
            macro_thesis=args.macro_thesis,
            trends=args.trends, theses=args.theses,
            philosophy=args.philosophy, open_questions=args.open_questions,
        )
        if path:
            print(str(path))
        else:
            print("(slide not produced — both components missing)", file=sys.stderr)
            sys.exit(2)

    asyncio.run(run())


if __name__ == "__main__":
    _main()
