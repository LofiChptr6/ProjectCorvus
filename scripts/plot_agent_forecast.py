"""Per-agent forecast plotter (v2 — 30-panel grid, in-panel annotation, agent commentary).

Each sector agent has its own bootstrap quant model in agents/<name>/models/.
This script loads the agent's universe (~30 tickers), runs that model on each
name in parallel, and renders a fixed 6×5 grid where each panel shows:
  - Price line + agent-specific overlays (SMAs / Bollinger / breakout level / etc.)
  - Forecast vector (dashed) sized by model's expected_return_pct over horizon
  - Inside-panel annotation overlay with:
      * agent-flavored commentary (uses sector vocabulary)
      * conviction derivation math (the formula that produced ER and conv)
      * inverse-ETF vehicle for any short forecast (looked up from
        agents/inverse_etf_map.yaml)

History window is 2× plot window so SMA200 (or any long indicator) is fully
warmed up before the visible region starts.

Usage:
    .venv/bin/python scripts/plot_agent_forecast.py <agent>            # save to /tmp/<agent>_forecast.png
    .venv/bin/python scripts/plot_agent_forecast.py <agent> --send     # also send to Telegram
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(str(ROOT / ".env"))


# ─────────────────────────────────────────────────────────────────────────────
# Per-agent commentary functions — each takes the model output and returns
# a 2-line string in the agent's domain vocabulary. First line = the math
# (conviction derivation). Second line = the call in agent-flavored prose.
# ─────────────────────────────────────────────────────────────────────────────

def _commentary_fab(m: dict) -> tuple[str, str]:
    inp = m.get("inputs", {})
    score = inp.get("score", 0)
    sp = inp.get("spread_pct_of_price", 0)
    sl = inp.get("slope_pct_of_price", 0)
    derivation = f"score = 0.6×{sp:+.2f}% + 0.4×{sl:+.3f}%×5 = {score:+.2f}  |  thr ±0.40"
    if m["direction"] == "long":
        prose = f"capex cycle expanding — long ER={m['expected_return_pct']:+.1f}%/90d (conv {m['conviction']:.3f})"
    elif m["direction"] == "short":
        prose = f"capex cycle rolling — bearish ER={m['expected_return_pct']:+.1f}%/90d (conv {m['conviction']:.3f})"
    else:
        prose = "spread+slope below threshold — no capex inflection signal"
    return derivation, prose


def _commentary_fabless(m: dict) -> tuple[str, str]:
    inp = m.get("inputs", {})
    pct_above = inp.get("pct_above_sma20", 0)
    slope = inp.get("sma20_slope_5bar", 0)
    derivation = f"%>SMA20={pct_above:+.2f}%  slope_5bar={slope:+.3f}  → ER={m.get('expected_return_pct', 0):+.1f}%/14d"
    if m["direction"] == "long":
        prose = f"design-win momentum strong — long {m['conviction']:.3f}"
    elif m["direction"] == "short":
        prose = f"momentum rolling, design wins fading — short {m['conviction']:.3f}"
    else:
        prose = "momentum stalled — no design-win edge"
    return derivation, prose


def _commentary_iron(m: dict) -> tuple[str, str]:
    return _commentary_fab(m)  # same indicator family (50/200 spread + slope)


def _commentary_atlas(m: dict) -> tuple[str, str]:
    inp = m.get("inputs", {})
    score = inp.get("score", 0)
    derivation = f"regime score = {score:+.2f}  (close>SMA200:{inp.get('above_sma200', '?')}, golden:{inp.get('golden_cross', '?')})"
    if m["direction"] == "long":
        prose = f"regime constructive — long ER={m['expected_return_pct']:+.1f}%/{m.get('time_to_target_days', 60)}d"
    elif m["direction"] == "short":
        prose = f"regime cracked — bearish ER={m['expected_return_pct']:+.1f}%/{m.get('time_to_target_days', 60)}d"
    else:
        prose = "transitional regime — stand aside"
    return derivation, prose


def _commentary_maya(m: dict) -> tuple[str, str]:
    inp = m.get("inputs", {})
    z = inp.get("z", 0)
    mu = inp.get("mean_20", 0)
    derivation = f"z = (last - μ20)/σ20 = {z:+.2f}  (μ ${mu:.2f})"
    if m["direction"] == "long":
        prose = f"oversold fade — long mean-reversion {m['conviction']:.3f}"
    elif m["direction"] == "short":
        prose = f"overbought fade — bearish mean-reversion {m['conviction']:.3f}"
    else:
        prose = "|z|<2 — inside the band, no fade"
    return derivation, prose


def _commentary_rex(m: dict) -> tuple[str, str]:
    inp = m.get("inputs", {})
    above = inp.get("pct_above_prior_high", 0)
    vol = inp.get("volume_ratio", 0)
    derivation = f"{above:+.2f}% above 20-bar high  ×  vol {vol:.1f}× avg"
    if m["direction"] == "long":
        prose = f"breakout intact, volume confirms — long {m['conviction']:.3f}"
    elif m["direction"] == "short":
        prose = f"failed breakout — short {m['conviction']:.3f}"
    else:
        prose = "no breakout setup — coiled"
    return derivation, prose


def _commentary_vera(m: dict) -> tuple[str, str]:
    inp = m.get("inputs", {})
    r5 = inp.get("range_5", 0)
    r20 = inp.get("range_20_avg", 0)
    ratio = inp.get("expansion_ratio", 0)
    derivation = f"range expansion = {ratio:.2f}× (5-bar avg ${r5:.2f} vs 20-bar avg ${r20:.2f})"
    if ratio > 1.5:
        prose = "vol expanding — IV-crush setup live"
    else:
        prose = "vol benign — no catalyst priced in"
    return derivation, prose


def _commentary_trump(m: dict) -> tuple[str, str]:
    inp = m.get("inputs", {})
    pct = inp.get("pct_today", 0)
    derivation = f"today's bar: {pct:+.2f}%"
    prose = "headline-driven; review news + age"
    return derivation, prose


def _commentary_volt(m: dict) -> tuple[str, str]:
    inp = m.get("inputs", {})
    corr = inp.get("corr_tlt_60", 0)
    derivation = f"60-bar corr w/ TLT = {corr:+.2f}"
    if m["direction"] == "long":
        prose = f"rate-sensitive bid — long {m['conviction']:.3f}"
    elif m["direction"] == "short":
        prose = f"yields rising, duration hit — short {m['conviction']:.3f}"
    else:
        prose = "duration neutral"
    return derivation, prose


# ─────────────────────────────────────────────────────────────────────────────
# Per-agent plot recipes
# ─────────────────────────────────────────────────────────────────────────────

AGENT_PLOT = {
    "fab": {
        "model_path": "agents.fab.models.equipment_cycle",
        "model_summary": "50/200 SMA spread + 20-bar slope (Coppock-style capex cycle)",
        "overlays": ["SMA_50", "SMA_200"],
        "history_window_days": 730,
        "plot_window_days": 365,
        "horizon_days": 90,
        "commentary": _commentary_fab,
    },
    "fabless": {
        "model_path": "agents.fabless.models.design_win_momentum",
        "model_summary": "20-bar SMA + last-close % distance (designer momentum)",
        "overlays": ["SMA_20"],
        "history_window_days": 180,
        "plot_window_days": 90,
        "horizon_days": 14,
        "commentary": _commentary_fabless,
    },
    "iron": {
        "model_path": "agents.iron.models.cycle_momentum",
        "model_summary": "50/200 SMA slope (industrial cycle momentum)",
        "overlays": ["SMA_50", "SMA_200"],
        "history_window_days": 730,
        "plot_window_days": 365,
        "horizon_days": 90,
        "commentary": _commentary_iron,
    },
    "atlas": {
        "model_path": "agents.atlas.models.regime_score",
        "model_summary": "Regime score: close vs SMA200 + SMA50/200 cross + SMA20 slope",
        "overlays": ["SMA_20", "SMA_50", "SMA_200"],
        "history_window_days": 730,
        "plot_window_days": 365,
        "horizon_days": 60,
        "commentary": _commentary_atlas,
    },
    "maya": {
        "model_path": "agents.maya.models.zscore_revert",
        "model_summary": "20-bar z-score: |z|>2 = mean-reversion setup",
        "overlays": ["SMA_20", "BBAND_20_2"],
        "history_window_days": 90,
        "plot_window_days": 45,
        "horizon_days": 5,
        "commentary": _commentary_maya,
    },
    "rex": {
        "model_path": "agents.rex.models.breakout_strength",
        "model_summary": "20-bar prior high break + volume surge",
        "overlays": ["HIGH_20", "VOL_BARS"],
        "history_window_days": 90,
        "plot_window_days": 45,
        "horizon_days": 7,
        "commentary": _commentary_rex,
    },
    "vera": {
        "model_path": "agents.vera.models.iv_crush_setup",
        "model_summary": "5-bar vs 20-bar range (realized-vol expansion proxy)",
        "overlays": ["RANGE_5_BAND"],
        "history_window_days": 90,
        "plot_window_days": 45,
        "horizon_days": 5,
        "commentary": _commentary_vera,
    },
    "volt": {
        "model_path": "agents.volt.models.rate_duration",
        "model_summary": "60-bar corr w/ TLT × 50-bar own deviation",
        "overlays": ["SMA_50"],
        "history_window_days": 365,
        "plot_window_days": 180,
        "horizon_days": 30,
        "commentary": _commentary_volt,
    },
    "trump": {
        "model_path": "agents.trump.models.headline_freshness",
        "model_summary": "Latest-bar % move + headline age (event-driven)",
        "overlays": ["SMA_20"],
        "history_window_days": 60,
        "plot_window_days": 30,
        "horizon_days": 3,
        "commentary": _commentary_trump,
    },
}


def parse_t(t):
    if "T" in t:
        return datetime.fromisoformat(t).date()
    return date.fromisoformat(t)


def sma(closes: list[float], n: int, idx: int) -> float | None:
    if idx + 1 < n:
        return None
    return sum(closes[idx + 1 - n: idx + 1]) / n


def stdev_at(closes: list[float], n: int, idx: int) -> float | None:
    if idx + 1 < n:
        return None
    window = closes[idx + 1 - n: idx + 1]
    m = sum(window) / n
    var = sum((c - m) ** 2 for c in window) / n
    return var ** 0.5


def load_universe(agent_name: str) -> list[str]:
    sm = yaml.safe_load((ROOT / "agents" / "sector_map.yaml").read_text())
    spec = (sm.get("agents") or {}).get(agent_name, {})
    return list((spec.get("universe") or {}).keys())


_INVERSE_MAP_CACHE = None
def lookup_inverse_for(symbol: str) -> str | None:
    global _INVERSE_MAP_CACHE
    if _INVERSE_MAP_CACHE is None:
        path = ROOT / "agents" / "inverse_etf_map.yaml"
        if not path.exists():
            _INVERSE_MAP_CACHE = {}
        else:
            data = yaml.safe_load(path.read_text()) or {}
            _INVERSE_MAP_CACHE = data.get("inverses") or {}
    sym = symbol.upper()
    candidates = []
    for inv_sym, meta in _INVERSE_MAP_CACHE.items():
        if not (meta or {}).get("verified"):
            continue
        if str((meta or {}).get("underlying", "")).upper() == sym:
            candidates.append((inv_sym, abs(float(meta.get("leverage", 0)))))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1])
    return candidates[0][0]


async def fetch_one(get_bars, sym: str, duration: str) -> tuple[str, list | Exception]:
    try:
        b = await get_bars(sym, "1 day", duration)
        return sym, b["bars"]
    except Exception as e:
        return sym, e


async def plot_for_agent(agent_name: str, send_telegram: bool = False,
                          fundamental_notes: dict[str, str] | None = None,
                          dpi: int = 240) -> str:
    cfg = AGENT_PLOT.get(agent_name)
    if cfg is None:
        raise SystemExit(f"No plot recipe for {agent_name!r}")

    model_mod = importlib.import_module(cfg["model_path"])
    compute = model_mod.compute
    commentary_fn = cfg["commentary"]

    universe = load_universe(agent_name)
    if not universe:
        raise SystemExit(f"No universe for {agent_name}")

    from data.massive_client import get_bars, aclose

    history_days = cfg["history_window_days"]
    plot_days = cfg["plot_window_days"]
    horizon = cfg["horizon_days"]

    if history_days >= 365:
        years = history_days // 365 + (1 if history_days % 365 else 0)
        duration = f"{years} Y"
    else:
        duration = f"{history_days} D"

    # Parallel bar fetches
    print(f"Fetching {len(universe)} bar series ({duration})...", flush=True)
    fetches = await asyncio.gather(*(fetch_one(get_bars, sym, duration) for sym in universe))
    bars_by_sym = {sym: result for sym, result in fetches}
    fetch_errors = sum(1 for r in bars_by_sym.values() if isinstance(r, Exception))
    print(f"Fetched {len(bars_by_sym) - fetch_errors}/{len(universe)} successfully")

    # Layout: 6 cols × 5 rows for 30 tickers (squarish, fits in Telegram limits)
    n = len(universe)
    cols = 6 if n >= 25 else 5 if n >= 16 else 4
    rows = (n + cols - 1) // cols

    panel_w = 4.0
    panel_h = 3.4
    fig, axes = plt.subplots(rows, cols, figsize=(panel_w * cols, panel_h * rows))
    # Tighter grid + extra top-pad inside each panel reserves room for the
    # fundamental-note header above the chart (added below per-panel).
    plt.subplots_adjust(left=0.035, right=0.99, top=0.945, bottom=0.04, hspace=0.45, wspace=0.18)
    axes = axes.flatten() if hasattr(axes, "flatten") else [axes]

    summaries = []

    for i, sym in enumerate(universe):
        ax = axes[i]
        bars = bars_by_sym.get(sym)
        if isinstance(bars, Exception) or bars is None:
            ax.text(0.5, 0.5, f"{sym}\nfetch failed", ha="center", va="center",
                    transform=ax.transAxes, fontsize=10)
            ax.axis("off")
            continue
        if len(bars) < 22:
            ax.text(0.5, 0.5, f"{sym}\nonly {len(bars)} bars", ha="center", va="center",
                    transform=ax.transAxes, fontsize=10)
            ax.axis("off")
            continue

        model_out = compute(sym, bars, {})
        summaries.append({"sym": sym, **model_out})

        all_dates = [parse_t(b["t"]) for b in bars]
        all_closes = [float(b["c"]) for b in bars]
        cutoff = all_dates[-1] - timedelta(days=plot_days)
        try:
            plot_start = next(j for j, d in enumerate(all_dates) if d >= cutoff)
        except StopIteration:
            plot_start = 0
        plot_dates = all_dates[plot_start:]
        plot_closes = all_closes[plot_start:]

        # Price line
        ax.plot(plot_dates, plot_closes, "b-", linewidth=1.0, label="close")

        # Overlays
        for ov in cfg["overlays"]:
            if ov.startswith("SMA_"):
                n_w = int(ov.split("_")[1])
                series = [sma(all_closes, n_w, j) for j in range(len(all_closes))]
                col = {"SMA_20": "#ff8c00", "SMA_50": "#1a8c1a", "SMA_200": "#cc1f1a"}.get(ov, "purple")
                ax.plot(plot_dates, series[plot_start:], "-", color=col, linewidth=0.9,
                        alpha=0.8, label=ov.replace("_", " "))
            elif ov == "BBAND_20_2":
                upper, lower = [], []
                for j in range(len(all_closes)):
                    m = sma(all_closes, 20, j)
                    s = stdev_at(all_closes, 20, j)
                    if m is not None and s is not None:
                        upper.append(m + 2 * s); lower.append(m - 2 * s)
                    else:
                        upper.append(None); lower.append(None)
                ax.plot(plot_dates, upper[plot_start:], "--", color="#888", alpha=0.6,
                        linewidth=0.7, label="±2σ")
                ax.plot(plot_dates, lower[plot_start:], "--", color="#888", alpha=0.6, linewidth=0.7)
            elif ov == "HIGH_20":
                bars_h = [b.get("h") for b in bars]
                hi = []
                for j in range(len(bars_h)):
                    if j < 20:
                        hi.append(None)
                    else:
                        hi.append(max(h for h in bars_h[j - 20:j] if h is not None))
                ax.plot(plot_dates, hi[plot_start:], "-", color="#aa3333", alpha=0.5,
                        linewidth=0.7, label="20-bar high")
            elif ov == "VOL_BARS":
                ax2 = ax.twinx()
                vols = [b.get("v") or 0 for b in bars[plot_start:]]
                if vols:
                    ax2.bar(plot_dates, vols, color="#dddddd", alpha=0.35, width=1.0)
                    ax2.set_ylim(0, max(vols) * 4 if max(vols) > 0 else 1)
                    ax2.set_yticks([])
            elif ov == "LAST_10_HIGHS":
                last10 = bars[-10:]
                last10_d = [parse_t(b["t"]) for b in last10]
                ax.scatter(last10_d, [b["h"] for b in last10], color="#cc1f1a", s=10, marker="v")
                ax.scatter(last10_d, [b["l"] for b in last10], color="#1a8c1a", s=10, marker="^")
            elif ov == "RANGE_5_BAND":
                last5 = bars[-5:]
                last5_d = [parse_t(b["t"]) for b in last5]
                ax.fill_between(last5_d, [b["h"] for b in last5], [b["l"] for b in last5],
                                alpha=0.30, color="#aa55aa", label="last-5 range")

        # Forecast vector
        current = all_closes[-1]
        e_return = model_out.get("expected_return_pct", 0)
        direction = model_out.get("direction", "flat")
        ttd = model_out.get("time_to_target_days") or horizon
        forecast_end = all_dates[-1] + timedelta(days=ttd)
        forecast_price = current * (1 + e_return / 100)
        color = {"long": "#1a8c1a", "short": "#cc1f1a"}.get(direction, "#888888")

        inverse_vehicle = lookup_inverse_for(sym) if direction == "short" else None

        ax.plot([all_dates[-1], forecast_end], [current, forecast_price],
                linestyle="--", color=color, linewidth=1.7)
        ax.scatter([forecast_end], [forecast_price], s=40, color=color, zorder=5,
                   edgecolor="black", linewidth=0.4)
        endpoint_text = f"${forecast_price:.0f} ({e_return:+.1f}%)"
        if inverse_vehicle:
            endpoint_text += f" via {inverse_vehicle}"
        ax.annotate(endpoint_text, xy=(forecast_end, forecast_price),
                    xytext=(5, 0), textcoords="offset points",
                    fontsize=6.5, color=color, fontweight="bold", va="center")

        # Header: ticker + direction badge above the chart, color-coded.
        # The fundamental note is rendered as a separate text block below the
        # title (still ABOVE the plot area, not blocking the price line).
        dir_badge = {"long": "▲", "short": "▼", "flat": "─"}.get(direction, "?")
        ax.set_title(
            f"{sym}  {dir_badge} {direction.upper()}  conv {model_out.get('conviction', 0):.3f}",
            fontsize=10.5, fontweight="bold", loc="left", color=color, pad=30,
        )

        note = (fundamental_notes or {}).get(sym) or "(no fundamental note this run)"
        # Wrap to ~52 chars/line so it fits the panel width
        words = note.split()
        wrapped: list[str] = []
        cur: list[str] = []
        line_w = 0
        for w in words:
            if line_w + len(w) + 1 > 52 and cur:
                wrapped.append(" ".join(cur))
                cur = [w]; line_w = len(w)
            else:
                cur.append(w); line_w += len(w) + 1
        if cur:
            wrapped.append(" ".join(cur))
        if inverse_vehicle and direction == "short":
            wrapped.append(f"→ trade via {inverse_vehicle}")
        note_text = "\n".join(wrapped[:3])

        # Note sits in the title-pad region (reserved by pad=30 above), just
        # above the plot area. Doesn't block the chart.
        ax.text(0.0, 1.005, note_text, transform=ax.transAxes,
                fontsize=6.7, color="#222", va="bottom", ha="left",
                linespacing=1.32,
                bbox=dict(boxstyle="round,pad=0.30", facecolor="#fffce6",
                          edgecolor="#aaa", alpha=0.95))

        ax.legend(loc="upper left", fontsize=5.5, framealpha=0.7, ncol=2)
        ax.grid(alpha=0.2)
        ax.tick_params(labelsize=7)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b'%y"))
        ax.set_xlim(plot_dates[0] if plot_dates else all_dates[0],
                    forecast_end + timedelta(days=2))

    for j in range(n, len(axes)):
        axes[j].axis("off")

    fig.suptitle(
        f"{agent_name.upper()} — {cfg['model_summary']}\n"
        f"{datetime.now().strftime('%Y-%m-%d %H:%M ET')} | "
        f"history {history_days}d / plot {plot_days}d / forecast {horizon}d | "
        f"{len(summaries)} of {n} symbols modelled",
        fontsize=12, fontweight="bold", y=0.985,
    )

    out_path = f"/tmp/{agent_name}_forecast.png"
    plt.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    print(f"saved {out_path}")

    # Output table
    print(f"\nModel output ({agent_name}):")
    print(f"  {'sym':<6} {'dir':<5} {'conv':>6}  {'ER':>7}  {'horizon':>8}")
    longs = sum(1 for s in summaries if s.get("direction") == "long")
    shorts = sum(1 for s in summaries if s.get("direction") == "short")
    flats = sum(1 for s in summaries if s.get("direction") == "flat")
    for s in summaries:
        print(f"  {s['sym']:<6} {s.get('direction', '?'):<5} {s.get('conviction', 0):>6.3f}  "
              f"{s.get('expected_return_pct', 0):>+6.1f}%  {s.get('time_to_target_days', 0):>5}d")
    print(f"  ─── {longs} long, {shorts} short, {flats} flat")

    await aclose()

    if send_telegram:
        from approval.telegram import send_photo
        cap_lines = [
            f"{agent_name.upper()} — {cfg['model_summary']}",
            f"{longs} long / {shorts} short / {flats} flat across {len(summaries)} names",
        ]
        # Top-conviction picks for caption (max 8 to stay under 1024 char limit)
        top = sorted(summaries, key=lambda x: -abs(x.get("conviction", 0)))[:8]
        if top:
            cap_lines.append("")
            cap_lines.append("Top by |conv|:")
            for s in top:
                arr = "^" if s["direction"] == "long" else "v" if s["direction"] == "short" else "-"
                cap_lines.append(
                    f"  {s['sym']:<5} {arr} {s.get('conviction', 0):.3f}  "
                    f"ER {s.get('expected_return_pct', 0):+.1f}%/{s.get('time_to_target_days', 0)}d"
                )
        caption = "\n".join(cap_lines)[:1024]
        res = await send_photo(out_path, caption=caption)
        print(f"telegram: ok={res.get('ok') if res else None}")

    return out_path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("agent")
    p.add_argument("--send", action="store_true")
    args = p.parse_args()
    asyncio.run(plot_for_agent(args.agent, send_telegram=args.send))


if __name__ == "__main__":
    main()
