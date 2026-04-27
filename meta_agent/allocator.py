"""Conviction-driven portfolio allocator (sector-shard architecture, Stage 2).

Reads every agent's active conviction views from the agent_conviction table,
computes signed target weights per symbol, and emits the order deltas Mike
needs to place to move the desk from current to target.

The math is deliberately simple — the LLM (mike-allocator) is expected to
review the output before flipping to live mode, and the user can tune
parameters via update_strategic_change proposals.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass
class ConvictionView:
    agent_name: str
    symbol: str
    direction: str            # 'long' | 'short' | 'flat'
    conviction: float         # >= 0
    expected_return_pct: Optional[float] = None
    time_to_target_days: Optional[int] = None
    rationale: Optional[str] = None


@dataclass
class TargetWeights:
    weights: dict[str, float] = field(default_factory=dict)        # {symbol: signed_weight}
    contributors: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    # contributors[symbol] = [(agent, signed_weighted_conviction), ...]
    cash_weight: float = 0.0       # fraction of NAV to keep as cash (held back from deployment)
    cash_contributors: list[tuple[str, float]] = field(default_factory=list)
    # [(agent, weighted_conviction)] for the CASH bucket — surfaces who voted cash


def compute_target_weights(
    views: Iterable[ConvictionView],
    influence_weights: Optional[dict[str, float]] = None,
    gross_leverage: float = 1.0,
    max_per_symbol: float = 0.20,
    min_trade_threshold: float = 0.005,
) -> TargetWeights:
    """Aggregate signed convictions into normalized target weights.

    Args:
        views: All active (non-expired, non-flat) conviction rows.
        influence_weights: Per-agent multiplier (default 1.0). Cassidy's
            calibration audit can recommend adjustments here.
        gross_leverage: Total |weight| sum after normalization. 1.0 = no margin.
        max_per_symbol: Cap any single name at this fraction of NAV.
        min_trade_threshold: Drop names below this absolute weight.

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

        if abs(delta) / max(nav, 1.0) < min_trade_threshold:
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
