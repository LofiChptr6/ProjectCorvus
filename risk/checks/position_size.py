from pathlib import Path

import yaml

from risk.models import OrderRequest, AccountState, RiskResult, ALLOWED


def _agent_overrides(agent_name: str) -> dict:
    if not agent_name:
        return {}
    try:
        from agent.agent_registry import load_agent
        return load_agent(agent_name).get("risk_overrides", {}) or {}
    except Exception:
        return {}


_INVERSE_MAP_CACHE: dict | None = None
_INVERSE_MAP_MTIME: float = 0.0


def _load_inverse_map() -> dict:
    """Cached load of agents/inverse_etf_map.yaml. Used to normalize per-symbol
    caps to per-underlying caps so a bug in allocator.net_inverse_pairs can't
    let a long-and-short pair on the same underlying both pass."""
    global _INVERSE_MAP_CACHE, _INVERSE_MAP_MTIME
    p = Path("agents") / "inverse_etf_map.yaml"
    if not p.exists():
        return {}
    mt = p.stat().st_mtime
    if _INVERSE_MAP_CACHE is None or mt != _INVERSE_MAP_MTIME:
        with open(p, "r", encoding="utf-8") as f:
            _INVERSE_MAP_CACHE = yaml.safe_load(f) or {}
        _INVERSE_MAP_MTIME = mt
    return _INVERSE_MAP_CACHE


def _underlying_and_leverage(symbol: str) -> tuple[str, float]:
    """If `symbol` is a verified inverse ETF, return (underlying, leverage).
    Otherwise return (symbol, +1.0) — i.e. it represents itself at 1x long."""
    sym = (symbol or "").upper()
    inverses = (_load_inverse_map() or {}).get("inverses") or {}
    entry = inverses.get(sym)
    if entry and entry.get("verified") is True:
        und = str(entry.get("underlying") or sym).upper()
        lev = float(entry.get("leverage") or 1.0)
        return und, lev
    return sym, 1.0


def check(order: OrderRequest, account: AccountState, cfg: dict) -> RiskResult:
    global_max_pct = cfg.get("risk", {}).get("max_position_pct", 0.20)
    overrides = _agent_overrides(order.agent_name)
    agent_max_pct = overrides.get("max_position_pct")
    max_pct = min(global_max_pct, agent_max_pct) if agent_max_pct is not None else global_max_pct
    if account.nav <= 0:
        return ALLOWED

    price = order.effective_price
    if not price or price <= 0:
        return ALLOWED  # Can't check without price; market orders pass

    # Current position in this symbol
    existing_qty = 0.0
    for pos in account.positions:
        if pos.get("symbol") == order.symbol:
            existing_qty = abs(pos.get("quantity", 0))
            break

    new_total_qty = existing_qty + order.quantity if order.action == "BUY" else existing_qty
    new_notional = new_total_qty * price
    new_pct = new_notional / account.nav

    if new_pct > max_pct:
        return RiskResult(
            allowed=False,
            reason=(
                f"Position in {order.symbol} would be {new_pct:.1%} of NAV "
                f"(limit {max_pct:.0%}). New notional: ${new_notional:,.0f}, NAV: ${account.nav:,.0f}."
            ),
            check_name="position_size",
        )

    # Defense-in-depth: also enforce the cap on the *underlying* economic exposure
    # so a long underlying + long inverse can't slip past via two separate per-symbol
    # checks. The allocator's net_inverse_pairs should already collapse this; we
    # catch the leak if it ever fails.
    und_sym, order_lev = _underlying_and_leverage(order.symbol)
    if und_sym != order.symbol.upper():
        # Convert order quantity into underlying-equivalent dollars
        order_signed_qty = order.quantity if order.action == "BUY" else -order.quantity
        order_underlying_dollars = order_signed_qty * price * order_lev
        # Sum existing exposures: this symbol's siblings (other inverses on same underlying)
        # plus the underlying itself. Each gets normalized by its own leverage.
        und_exposure = 0.0
        for pos in account.positions:
            sym = (pos.get("symbol") or "").upper()
            qty = float(pos.get("quantity") or 0.0)
            mv = float(pos.get("market_value") or 0.0)
            if not sym or qty == 0 or mv == 0:
                continue
            und, lev = _underlying_and_leverage(sym)
            if und != und_sym:
                continue
            # market_value is signed by holding direction (long > 0); multiply by leverage
            # to get underlying-equivalent dollars.
            und_exposure += mv * lev
        # Note: we add (not subtract for SELL) because the order's signed quantity
        # already encodes side. Compare net underlying exposure against cap.
        net = abs(und_exposure + order_underlying_dollars)
        net_pct = net / account.nav
        if net_pct > max_pct:
            return RiskResult(
                allowed=False,
                reason=(
                    f"Underlying exposure to {und_sym} (via {order.symbol} + held inverses) "
                    f"would be {net_pct:.1%} of NAV (limit {max_pct:.0%}). "
                    f"This usually means allocator.net_inverse_pairs failed to collapse a long/short pair."
                ),
                check_name="position_size_underlying",
            )
    return ALLOWED
