"""Conviction-driven portfolio allocator (sector-shard architecture, Stage 2).

Reads every agent's active conviction views from the agent_conviction table,
computes signed target weights per symbol, and emits the order deltas Mike
needs to place to move the desk from current to target.

The math is deliberately simple — the LLM (mike-allocator) is expected to
review the output before flipping to live mode, and the user can tune
parameters via update_strategic_change proposals.

OPTIONAL mixture-then-functional path (Phase E): when env
`ALLOC_USE_MIXTURE=1`, `enrich_views_with_mixture` replaces per-agent scalar
votes on symbols that have active distributions in agent_forecast with a
single mixture-derived scalar (the conviction functional applied to the
per-symbol weighted mixture of distributions). Symbols with no distributions
stay on the legacy scalar-sum path. Disabled by default.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from typing import Iterable, Optional

log = logging.getLogger(__name__)


@dataclass
class ConvictionView:
    agent_name: str
    symbol: str
    direction: str            # 'long' | 'short' | 'flat'
    conviction: float         # >= 0
    expected_return_pct: Optional[float] = None
    time_to_target_days: Optional[int] = None
    rationale: Optional[str] = None
    # For direction='long' on a verified inverse-ETF symbol, the agent's self-
    # assertion: True ⇒ underlying already showing the bearish move (allocator
    # auto-places). False / None ⇒ early entry (allocator queues for Telegram
    # approval). Ignored on non-inverse symbols.
    momentum_confirmed: Optional[bool] = None
    # Defensive auto-flat trigger: if the position's unrealized return falls
    # below -stop_pct, apply_safety_brakes() will downgrade the view to flat.
    # Per-symbol; agents set this on inverse-ETF longs especially. None ⇒ no stop.
    stop_pct: Optional[float] = None


def apply_safety_brakes(
    views: Iterable["ConvictionView"],
    current_positions: dict[str, dict],
) -> tuple[list["ConvictionView"], list[dict]]:
    """Downgrade views to flat where defensive triggers fire.

    Currently checks only `stop_pct`: for each view with stop_pct set, look up
    the symbol's avg_cost and current market_value-implied price in
    current_positions; if the unrealized return is worse than -stop_pct, the
    view is replaced with a direction='flat' / conviction=0 row so the
    allocator closes the position rather than continuing to express it.

    Returns (filtered_views, brake_log). brake_log records each fired trigger
    so allocator output can attribute the close to the safety brake rather
    than to the agent voluntarily flatting.
    """
    out: list[ConvictionView] = []
    log: list[dict] = []

    for v in views:
        sym = (v.symbol or "").upper()
        stop = v.stop_pct
        if stop is None or stop <= 0 or v.direction == "flat" or v.conviction <= 0:
            out.append(v)
            continue

        cur = current_positions.get(sym) or current_positions.get(sym.lower()) or {}
        avg_cost = float(cur.get("avg_cost") or 0.0)
        qty = float(cur.get("position") or 0.0)
        mkt_val = float(cur.get("market_value") or 0.0)
        if avg_cost <= 0 or qty <= 0:
            # No position yet (or shorts not handled here) — nothing to brake.
            out.append(v)
            continue

        # Implied current price from market_value / qty. We compute return on
        # cost basis directly so this works whether market_value was filled in
        # by quotes or read off positions.
        cost_basis = avg_cost * qty
        if cost_basis <= 0:
            out.append(v)
            continue
        unrealized_pct = ((mkt_val - cost_basis) / cost_basis) * 100.0

        if unrealized_pct < -float(stop):
            log.append({
                "agent": v.agent_name, "symbol": sym, "direction": v.direction,
                "stop_pct": float(stop), "unrealized_pct": unrealized_pct,
                "trigger": "stop_pct",
            })
            out.append(ConvictionView(
                agent_name=v.agent_name, symbol=v.symbol, direction="flat",
                conviction=0.0, expected_return_pct=None,
                time_to_target_days=None, rationale=v.rationale,
                momentum_confirmed=None, stop_pct=v.stop_pct,
            ))
            continue

        out.append(v)

    return out, log


@dataclass
class TargetWeights:
    weights: dict[str, float] = field(default_factory=dict)        # {symbol: signed_weight}
    contributors: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    # contributors[symbol] = [(agent, signed_weighted_conviction), ...]
    cash_weight: float = 0.0       # fraction of NAV to keep as cash (held back from deployment)
    cash_contributors: list[tuple[str, float]] = field(default_factory=list)
    # [(agent, weighted_conviction)] for the CASH bucket — surfaces who voted cash


def use_mixture_enabled() -> bool:
    """Phase-E flag: when ALLOC_USE_MIXTURE=1, the rebalance path runs
    distribution-mixer per-symbol substitution. Default off until A/B has
    validated the path against the legacy scalar-sum allocator."""
    return os.environ.get("ALLOC_USE_MIXTURE", "0").lower() in {"1", "true", "yes", "on"}


async def enrich_views_with_mixture(
    views: list["ConvictionView"],
    influence_weights: Optional[dict[str, float]] = None,
    functional_name: Optional[str] = None,
) -> tuple[list["ConvictionView"], dict[str, dict]]:
    """For every symbol with active distributions in agent_forecast, replace
    the per-agent scalar conviction rows with N synthetic rows (one per
    contributor) whose convictions sum to the mixture-derived scalar.

    Rationale: the mixture-then-functional path preserves orthogonal
    distributional info across agents. Substituting at the ConvictionView
    layer keeps downstream contributors / attribution working — each
    contributing agent still sees a row, just with a scalar derived from the
    mixture instead of their own LLM-emitted number.

    Args:
        views: raw per-agent conviction rows from get_active_convictions
        influence_weights: per-agent multiplier passed through to the mixer's
            weighting (calibration_skill × influence × 1/√t_days)
        functional_name: which functional to apply to each mixture; defaults to
            meta_agent.conviction_functionals.DEFAULT_FUNCTIONAL

    Returns:
        (new_views, report) — new_views is the substituted list, ready for
        compute_target_weights. report is a per-symbol dict with the legacy
        scalar sum and the mixture-derived scalar for A/B logging.

    Symbols with no distributions are left untouched.
    """
    from db import store
    from meta_agent import distribution_mixer

    influence_weights = influence_weights or {}

    # Pre-fetch all active distribution rows in one query; bucket by symbol.
    dist_rows = await store.get_active_distributions(symbol=None)
    by_symbol: dict[str, list[dict]] = {}
    for r in dist_rows:
        # Distribution may come back as JSON string from asyncpg if it's a
        # JSONB column; the mixer expects a dict.
        d = r.get("distribution")
        if isinstance(d, str):
            import json as _json
            try:
                r["distribution"] = _json.loads(d)
            except _json.JSONDecodeError:
                continue
        by_symbol.setdefault(r["symbol"].upper(), []).append(r)

    report: dict[str, dict] = {}
    out_views: list[ConvictionView] = []
    consumed_symbols: set[str] = set()

    for sym, rows in by_symbol.items():
        res = distribution_mixer.mixture_conviction(
            rows, influence_by_agent=influence_weights,
            functional_name=functional_name,
        )
        if res is None:
            continue
        direction, scalar, contributors = res
        if scalar <= 0 or direction == "flat":
            # Mixture says flat/zero — leave the original per-agent scalar views
            # in place so the legacy scalar-sum path can still decide. Record
            # the verdict for A/B reporting only.
            report[sym] = {
                "mixture_direction": direction,
                "mixture_scalar":    scalar,
                "contributors":      contributors,
                "n_distributions":   len(rows),
                "substituted":       False,
                "reason":            "flat-or-zero",
            }
            continue
        total_w = sum(w for _, w in contributors) or 1.0
        for agent_name, w in contributors:
            out_views.append(ConvictionView(
                agent_name=agent_name, symbol=sym, direction=direction,
                conviction=float(scalar * (w / total_w)),
                expected_return_pct=None, time_to_target_days=None,
                rationale=f"[mixture] {len(rows)} dists; functional applied",
                momentum_confirmed=None, stop_pct=None,
            ))
        consumed_symbols.add(sym)
        report[sym] = {
            "mixture_direction": direction,
            "mixture_scalar":    scalar,
            "contributors":      contributors,
            "n_distributions":   len(rows),
            "substituted":       True,
        }

    # For symbols we substituted, drop the original per-agent scalar rows so
    # they don't double-count alongside the mixture-derived rows.
    for v in views:
        sym = (v.symbol or "").upper()
        if sym in consumed_symbols:
            # Compute legacy scalar sum for A/B reporting before dropping.
            entry = report.setdefault(sym, {})
            legacy = entry.get("legacy_sum", 0.0)
            sign = 1 if v.direction == "long" else (-1 if v.direction == "short" else 0)
            legacy += sign * float(v.conviction)
            entry["legacy_sum"] = legacy
            continue
        out_views.append(v)

    log.info("mixture path: %d symbols substituted, %d untouched",
             sum(1 for r in report.values() if r.get("substituted")),
             len([v for v in views if (v.symbol or '').upper() not in consumed_symbols]))
    return out_views, report


def compute_conviction(
    expected_return_pct: Optional[float],
    likelihood: Optional[float],
    time_to_target_days: Optional[float],
) -> float:
    """Central conviction formula — the ONLY place a conviction value is derived.

    conviction = abs(expected_return_pct) × likelihood / time_to_target_days

    Inputs:
        expected_return_pct: signed %% move (sign is carried by `direction`;
            magnitude used here). e.g. +5.0 for +5%%, -8.0 for -8%%.
        likelihood: forecast probability in [0, 1]. 0 → conviction 0 (no edge);
            1 → full-confidence call.
        time_to_target_days: trading-day horizon, must be > 0.

    Returns a positive float (unbounded — short horizons + large moves
    naturally produce higher convictions). The allocator's per-symbol
    sum-of-abs normalization makes the absolute value moot; only the
    cross-symbol ranking + relative magnitudes matter.

    Returns 0.0 on any invalid input (caller treats as flat).
    """
    if expected_return_pct is None or likelihood is None or time_to_target_days is None:
        return 0.0
    try:
        er = abs(float(expected_return_pct))
        lk = float(likelihood)
        ttd = float(time_to_target_days)
    except (TypeError, ValueError):
        return 0.0
    if lk <= 0.0 or ttd <= 0.0:
        return 0.0
    if lk > 1.0:
        lk = 1.0  # clamp; agents that emit >1 are mis-calibrating, not earning a bonus
    return er * lk / ttd


def compute_target_weights(
    views: Iterable[ConvictionView],
    influence_weights: Optional[dict[str, float]] = None,
    gross_leverage: float = 2.0,
    max_per_symbol: float = 0.40,
    min_trade_threshold: float = 0.002,
    top_n: Optional[int] = 30,
) -> TargetWeights:
    """Aggregate signed convictions into normalized target weights.

    Args:
        views: All active (non-expired, non-flat) conviction rows.
        influence_weights: Per-agent multiplier (default 1.0). Cassidy's
            calibration audit can recommend adjustments here.
        gross_leverage: Total |weight| sum after normalization. 1.0 = no margin;
            >1.0 uses margin. Default 2.0 (2x leverage).
        max_per_symbol: Cap any single name at this fraction of NAV. Default 0.40.
        min_trade_threshold: Drop names below this absolute weight. Default 0.002.
        top_n: Keep only the top-N highest |signed-conviction| symbols (after
            agent aggregation). CASH is held aside before this trim and is
            ALWAYS retained. Set to None to disable. Default 30 — wide breadth
            across 11 sector agents.

    Returns:
        TargetWeights with `.weights` and `.contributors`.
    """
    influence = influence_weights or {}

    signed: dict[str, float] = {}
    contributors: dict[str, list[tuple[str, float]]] = {}

    for v in views:
        sign = +1 if v.direction == "long" else -1 if v.direction == "short" else 0
        if sign == 0 or v.conviction <= 0:
            continue
        w = sign * float(v.conviction) * influence.get(v.agent_name, 1.0)
        sym = v.symbol.upper()
        signed[sym] = signed.get(sym, 0.0) + w
        contributors.setdefault(sym, []).append((v.agent_name, w))

    # Pull CASH out before normalization. CASH conviction is "I want to hold
    # cash" — competes with longs/hedges for share of NAV but doesn't generate
    # an order. Only positive (long) cash votes count; the validator rejects
    # short-on-CASH at the MCP layer.
    cash_w = signed.pop("CASH", 0.0)
    if cash_w < 0:
        cash_w = 0.0
    cash_contributors = contributors.pop("CASH", [])

    # Net out near-zero (long ≈ short cancels)
    signed = {s: w for s, w in signed.items() if abs(w) > 1e-6}
    if not signed and cash_w == 0:
        return TargetWeights(weights={}, contributors=contributors,
                             cash_weight=0.0, cash_contributors=cash_contributors)

    # Concentration trim: keep only the top-N |signed| symbols. CASH was pulled
    # out above and is always preserved. Drops the long tail of low-conviction
    # tickers that would otherwise produce sub-$500 fills hammering the
    # commission floor.
    if top_n is not None and len(signed) > top_n:
        ranked = sorted(signed.items(), key=lambda kv: abs(kv[1]), reverse=True)
        kept = dict(ranked[:top_n])
        # Drop dropped-symbol contributors so attribution stays consistent
        for sym in list(contributors.keys()):
            if sym != "CASH" and sym not in kept:
                contributors.pop(sym, None)
        signed = kept

    # Normalize so abs-weights (including the cash share) sum to gross_leverage.
    # The cash share doesn't deploy capital, so it pulls deployed gross down
    # proportionally — exactly the desired "vote cash to reduce risk" semantic.
    total_abs = sum(abs(w) for w in signed.values()) + cash_w
    if total_abs == 0:
        return TargetWeights(weights={}, contributors=contributors,
                             cash_weight=0.0, cash_contributors=cash_contributors)
    scale = gross_leverage / total_abs
    target = {s: w * scale for s, w in signed.items()}
    cash_target = cash_w * scale

    # Per-symbol cap (does NOT redistribute the trimmed excess; it's a hard safety bound)
    target = {s: max(-max_per_symbol, min(max_per_symbol, w)) for s, w in target.items()}

    # Drop sub-threshold names
    target = {s: w for s, w in target.items() if abs(w) >= min_trade_threshold}

    return TargetWeights(weights=target, contributors=contributors,
                         cash_weight=cash_target, cash_contributors=cash_contributors)


# ── Net underlying-vs-inverse pairs ───────────────────────────────────────────

@dataclass
class NettedPair:
    underlying: str
    inverse: str
    leverage: float           # signed, e.g., -2.0
    gross_underlying: float   # weight before netting
    gross_inverse: float      # weight before netting
    net_underlying_equiv: float  # net economic exposure to underlying after offset
    kept: Optional[str]       # symbol kept after netting, or None if both dropped
    kept_weight: float        # weight of the kept position (0 if both dropped)


def net_inverse_pairs(
    target_weights: dict[str, float],
    contributors: dict[str, list[tuple[str, float]]],
    inverse_map: dict,
    min_residual: float = 1e-6,
) -> tuple[dict[str, float], dict[str, list[tuple[str, float]]], list[NettedPair]]:
    """Collapse (long underlying + long its inverse) pairs into single positions.

    For each underlying with both a positive long weight and one or more long
    inverse-ETF weights:
      - Compute net_underlying_equiv = und_w + sum(inv_w * leverage_i)
      - If net > 0:  keep underlying at net, drop all paired inverses
      - If net < 0:  keep one inverse (lowest |leverage| = least decay) sized
                     to express net_underlying_equiv via inv_w = net / leverage;
                     drop underlying and any other paired inverses
      - If |net| < min_residual: drop everything (positions cancel cleanly)

    Contributors are merged into the kept symbol so attribution survives.

    Args:
        target_weights: {symbol: signed_weight}
        contributors: {symbol: [(agent, signed_weighted_conviction), ...]}
        inverse_map: parsed agents/inverse_etf_map.yaml — {inverse_sym:
                     {underlying, leverage, ...}, ...}
        min_residual: weights smaller than this absolute value are dropped.

    Returns:
        (new_weights, new_contributors, [NettedPair logs])
    """
    out_w = dict(target_weights)
    out_c: dict[str, list[tuple[str, float]]] = {k: list(v) for k, v in contributors.items()}
    log: list[NettedPair] = []

    inverses_block = (inverse_map or {}).get("inverses") or {}
    # underlying -> [(inverse_sym, leverage), ...]
    by_underlying: dict[str, list[tuple[str, float]]] = {}
    for inv_sym, meta in inverses_block.items():
        und = (meta or {}).get("underlying")
        lev = (meta or {}).get("leverage")
        if not und or lev is None:
            continue
        by_underlying.setdefault(str(und).upper(), []).append((str(inv_sym).upper(), float(lev)))

    for und, inverses in by_underlying.items():
        if und not in out_w or out_w[und] <= 0:
            continue
        # Active inverses for this underlying that have positive weight in our basket
        active = [(inv, lev) for (inv, lev) in inverses if inv in out_w and out_w[inv] > 0]
        if not active:
            continue
        # Prefer keeping the lowest-|leverage| inverse if a net-bearish position results
        active.sort(key=lambda x: abs(x[1]))

        und_w = out_w[und]
        # Collect all underlying-equivalent contributions from inverses
        total_inv_und_equiv = sum(out_w[inv] * lev for (inv, lev) in active)
        net_und_equiv = und_w + total_inv_und_equiv

        # Build a single log entry per (underlying, sum-of-inverses) collapse.
        primary_inv, primary_lev = active[0]  # the one we'd keep if net is bearish
        gross_inv_total = sum(out_w[inv] for (inv, _) in active)

        # Pull contributors from underlying + all inverses into a single list
        merged_contribs = list(out_c.get(und, []))
        for (inv, _) in active:
            merged_contribs.extend(out_c.get(inv, []))

        if net_und_equiv > min_residual:
            # Net long underlying: keep underlying at net weight, drop inverses
            out_w[und] = net_und_equiv
            for (inv, _) in active:
                out_w.pop(inv, None)
                out_c.pop(inv, None)
            out_c[und] = merged_contribs
            log.append(NettedPair(
                underlying=und, inverse=",".join(inv for inv, _ in active),
                leverage=primary_lev,
                gross_underlying=und_w, gross_inverse=gross_inv_total,
                net_underlying_equiv=net_und_equiv,
                kept=und, kept_weight=net_und_equiv,
            ))
        elif net_und_equiv < -min_residual:
            # Net short underlying: keep ONE inverse (lowest |leverage|), size to express net
            new_inv_w = net_und_equiv / primary_lev  # negative / negative = positive
            out_w[primary_inv] = new_inv_w
            # Drop other inverses + the underlying
            out_w.pop(und, None)
            out_c.pop(und, None)
            for (inv, _) in active:
                if inv == primary_inv:
                    continue
                out_w.pop(inv, None)
                out_c.pop(inv, None)
            out_c[primary_inv] = merged_contribs
            log.append(NettedPair(
                underlying=und, inverse=",".join(inv for inv, _ in active),
                leverage=primary_lev,
                gross_underlying=und_w, gross_inverse=gross_inv_total,
                net_underlying_equiv=net_und_equiv,
                kept=primary_inv, kept_weight=new_inv_w,
            ))
        else:
            # Positions cancel within tolerance: drop everything
            out_w.pop(und, None)
            out_c.pop(und, None)
            for (inv, _) in active:
                out_w.pop(inv, None)
                out_c.pop(inv, None)
            log.append(NettedPair(
                underlying=und, inverse=",".join(inv for inv, _ in active),
                leverage=primary_lev,
                gross_underlying=und_w, gross_inverse=gross_inv_total,
                net_underlying_equiv=net_und_equiv,
                kept=None, kept_weight=0.0,
            ))

    return out_w, out_c, log


# ── Inverse-ETF detection + momentum gate ─────────────────────────────────────

def is_inverse_etf_symbol(symbol: str, inverse_map: dict) -> bool:
    """True iff `symbol` is a verified key under inverses: in
    agents/inverse_etf_map.yaml. Used by the allocator to decide which BUY
    orders need to flow through the momentum-confirmed approval gate."""
    if not symbol:
        return False
    inverses = (inverse_map or {}).get("inverses") or {}
    entry = inverses.get(symbol.upper())
    return bool(entry and entry.get("verified") is True)


def classify_inverse_order_gate(
    order_symbol: str,
    order_side: str,
    views: Iterable[ConvictionView],
    inverse_map: dict,
) -> tuple[str, list[ConvictionView]]:
    """Decide whether a proposed order needs human approval.

    Returns (decision, contributing_views) where decision is one of:
      - "auto"   — place this run without approval. Used for non-inverse
                   symbols, SELL orders (closes/reductions), and BUY orders on
                   inverse ETFs where every agent who took the bearish-via-
                   inverse position asserted momentum_confirmed=True.
      - "gated"  — queue for Telegram approval. Triggered when ANY contributing
                   long-inverse view has momentum_confirmed=False or None, or
                   when no long-inverse view exists (position arose purely from
                   netting and we can't audit the bearish call directly).

    contributing_views is the subset of views that took a long position on the
    order symbol — what the user reads on the Telegram approval prompt."""
    side = (order_side or "").upper()
    if side != "BUY" or not is_inverse_etf_symbol(order_symbol, inverse_map):
        return ("auto", [])
    sym = order_symbol.upper()
    contribs = [
        v for v in views
        if v.symbol.upper() == sym and v.direction == "long" and v.conviction > 0
    ]
    if not contribs:
        # No agent explicitly went long the inverse — position came purely from
        # the netting layer or other indirect path. Conservative: gate it.
        return ("gated", [])
    if all(v.momentum_confirmed is True for v in contribs):
        return ("auto", contribs)
    return ("gated", contribs)


# ── Bearish vehicle resolution ────────────────────────────────────────────────

def resolve_bearish_vehicle(
    symbol: str,
    sector_map: dict,
) -> tuple[str, str]:
    """Given a symbol with a *negative* target weight, return (vehicle, mode).

    Desk policy: NO DIRECT SHORTS. Bearish views may only be expressed via
    inverse-ETF mappings declared in sector_map.yaml. Names without an
    inverse_etf entry resolve to ("<symbol>", "skip"), and the allocator
    drops the order without trading.

    Returns:
        ("SQQQ", "inverse_etf")  — buy SQQQ to express bearish QQQ exposure
        ("AAPL", "skip")         — bearish view recorded but NOT tradeable;
                                    no order will be emitted.
    """
    sym = symbol.upper()
    agents = (sector_map or {}).get("agents") or {}
    for spec in agents.values():
        universe = spec.get("universe") or {}
        for s, meta in universe.items():
            if s.upper() != sym:
                continue
            via = (meta or {}).get("bearish_via", "skip")
            if isinstance(via, str) and via.startswith("inverse_etf:"):
                return (via.split(":", 1)[1].strip().upper(), "inverse_etf")
            return (sym, "skip")
    return (sym, "skip")


# ── Order diff: target_weights × NAV − current_positions → orders ─────────────

@dataclass
class ProposedOrder:
    symbol: str
    side: str        # 'BUY' | 'SELL'
    qty: int
    delta_value: float
    rationale: str


def diff_to_orders(
    target_weights: dict[str, float],
    current_positions: dict[str, dict],
    quotes: dict[str, float],
    nav: float,
    sector_map: Optional[dict] = None,
    min_trade_threshold: float = 0.005,
) -> list[ProposedOrder]:
    """Translate target weights into BUY/SELL orders.

    Args:
        target_weights: {symbol: signed_weight} (signed, fraction of NAV)
        current_positions: {symbol: {"position": shares, "market_value": $, "avg_cost": $}}
        quotes: {symbol: last_price}
        nav: Total account NAV (used to convert weight → dollars)
        sector_map: Optional, used to resolve bearish vehicles for negative weights.
        min_trade_threshold: Skip orders smaller than this fraction of NAV.

    Returns:
        list[ProposedOrder] — what Mike should send.
    """
    orders: list[ProposedOrder] = []

    # Step 1: For each negative target weight, translate to bearish vehicle.
    #   QQQ -0.10 → buy SQQQ at +0.10 (1x equivalent) OR short QQQ at -0.10.
    # We split the resolution into a working dict {actual_symbol: signed_weight_in_$}.
    target_dollars: dict[str, float] = {}
    rationale_map: dict[str, str] = {}

    for symbol, weight in target_weights.items():
        target_dollar = weight * nav
        if weight >= 0:
            target_dollars[symbol.upper()] = target_dollars.get(symbol.upper(), 0.0) + target_dollar
            rationale_map[symbol.upper()] = f"long target {weight:+.1%} of NAV"
        else:
            vehicle, mode = resolve_bearish_vehicle(symbol, sector_map or {})
            if mode == "inverse_etf":
                # Convert -X% bearish exposure on SYM → +X% on inverse ETF
                target_dollars[vehicle] = target_dollars.get(vehicle, 0.0) + abs(target_dollar)
                rationale_map[vehicle] = f"bearish {symbol} via {vehicle} ({weight:+.1%})"
            # mode == "skip": desk policy bans direct shorts. Bearish view is
            # recorded in agent_conviction but not translated into an order.

    # Step 2: Diff each symbol against current.
    all_symbols = set(target_dollars) | set(s.upper() for s in current_positions)

    for sym in all_symbols:
        target_value = target_dollars.get(sym, 0.0)
        cur = current_positions.get(sym) or current_positions.get(sym.lower()) or {}
        current_value = float(cur.get("market_value", 0.0))
        delta = target_value - current_value

        # Asymmetric threshold: opens respect min_trade_threshold (commission floor),
        # but full closes (target=0, current>0) bypass it. Hour-to-hour reconciliation
        # must be able to flatten sub-threshold orphan positions when no agent is
        # currently expressing a view; otherwise tiny inverse-ETF leftovers compound
        # decay losses indefinitely.
        is_full_close = target_value == 0.0 and current_value > 0.0
        if not is_full_close and abs(delta) / max(nav, 1.0) < min_trade_threshold:
            continue

        last = quotes.get(sym) or quotes.get(sym.lower())
        # NaN/Inf comparisons silently return False; check finiteness explicitly
        # before any arithmetic so a bad quote can't produce an oversized order.
        if not last or not math.isfinite(last) or last <= 0:
            continue
        if not math.isfinite(delta):
            continue
        qty = int(abs(delta) // last)
        if qty <= 0 or qty > 1_000_000:
            continue

        side = "BUY" if delta > 0 else "SELL"
        orders.append(ProposedOrder(
            symbol=sym,
            side=side,
            qty=qty,
            delta_value=delta,
            rationale=rationale_map.get(sym, f"rebalance {sym} to ${target_value:,.0f}"),
        ))

    # Sort by absolute dollar delta, biggest first (mike-allocator can stop early
    # if total notional risk for the run is hit).
    orders.sort(key=lambda o: abs(o.delta_value), reverse=True)
    return orders


# ── P&L attribution slicing ───────────────────────────────────────────────────

def split_attribution(
    contributors: list[tuple[str, float]],
    side: str,
) -> list[tuple[str, float]]:
    """Given the contributing (agent, signed_weight) tuples for a symbol and the
    direction Mike actually traded, return per-agent attribution shares
    (sum to 1.0) for the agents whose conviction matched the executed side.

    side: 'BUY' (long execution) or 'SELL' (sell/short execution).
    Only contributors whose weight matches the direction get credit; agents
    on the losing side of the consensus are NOT debited (their P&L is 0 for
    this fill — they just didn't move the desk this time).
    """
    if side == "BUY":
        relevant = [(a, w) for (a, w) in contributors if w > 0]
    else:
        relevant = [(a, w) for (a, w) in contributors if w < 0]

    total = sum(abs(w) for (_, w) in relevant)
    if total <= 0:
        return []
    return [(a, abs(w) / total) for (a, w) in relevant]
