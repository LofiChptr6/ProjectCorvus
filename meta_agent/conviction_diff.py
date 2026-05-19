"""Conviction-revision materiality.

Used by the worker to decide whether a completed `ocap_triggered_review`
needs to fire an `ocap_rebalance`. If an agent re-publishes a byte-identical
view, allocator would diff to zero orders, send a noisy Telegram, and burn
~3s for nothing. The materiality gate short-circuits that.

Materiality rules (deliberately conservative):
- `direction` changed (any prior-vs-new mismatch, including new symbol)
- `conviction` magnitude shifted by ≥ `CONVICTION_DELTA_THRESHOLD`
- `expected_return_pct` shifted by ≥ `ER_DELTA_THRESHOLD_PP`
- `stop_pct` presence flipped (None ↔ value) — turning on a stop or
  removing one is always material; small magnitude shifts within "with-stop"
  are not (they only matter at the band edge, which the allocator handles)
- Symbol was held prior but is absent from the new submission → implicit
  flat. Always material; means the agent backed off a position.
- Symbol is new (no prior row) → always material; fresh commitment.

Thresholds default to lenient — better to occasionally fire allocator on
a marginal revision than miss a real one. Tune in `risk` config if the
desk observes too many no-op rebalances.
"""
from __future__ import annotations

from typing import Optional


# Default thresholds. Conservative — wider thresholds = fewer allocator
# fires but higher risk of missing a real signal. Narrow these only after
# observation confirms the false-positive rate is high.
CONVICTION_DELTA_THRESHOLD = 0.05
ER_DELTA_THRESHOLD_PP = 1.0


def is_material(
    prior: Optional[dict],
    new: dict,
    *,
    conviction_delta: float = CONVICTION_DELTA_THRESHOLD,
    er_delta_pp: float = ER_DELTA_THRESHOLD_PP,
) -> tuple[bool, str]:
    """Compare a prior agent_conviction row with the proposed new fields.

    Args:
        prior: dict from agent_conviction (direction, conviction,
               expected_return_pct, stop_pct), or None if no prior row.
        new: dict with the new values being upserted. Same keys.

    Returns: (is_material, reason). Reason is a short human string for
        logging — "new" / "direction:long→flat" / "conv:0.40→0.65" / etc.
    """
    if prior is None:
        return True, "new"

    p_dir = (prior.get("direction") or "").lower()
    n_dir = (new.get("direction") or "").lower()
    if p_dir != n_dir:
        return True, f"direction:{p_dir}→{n_dir}"

    p_conv = float(prior.get("conviction") or 0.0)
    n_conv = float(new.get("conviction") or 0.0)
    if abs(p_conv - n_conv) >= conviction_delta:
        return True, f"conv:{p_conv:.2f}→{n_conv:.2f}"

    p_er = prior.get("expected_return_pct")
    n_er = new.get("expected_return_pct")
    if (p_er is None) != (n_er is None):
        return True, f"er_presence:{p_er!r}→{n_er!r}"
    if p_er is not None and n_er is not None:
        if abs(float(p_er) - float(n_er)) >= er_delta_pp:
            return True, f"er:{float(p_er):+.1f}→{float(n_er):+.1f}"

    p_stop = prior.get("stop_pct")
    n_stop = new.get("stop_pct")
    if (p_stop is None) != (n_stop is None):
        return True, f"stop_presence:{p_stop!r}→{n_stop!r}"

    return False, "no_material_change"


def count_material_changes(
    prior_rows: list[dict],
    new_resolved: list[dict],
) -> tuple[int, list[str]]:
    """Aggregate materiality across an agent's full submission set.

    Args:
        prior_rows: every row from store.get_agent_active_convictions
            before clear+upsert (one per symbol the agent held).
        new_resolved: every (resolved) new conviction the agent is about
            to upsert. Each dict must carry `symbol` plus the comparison
            fields used by `is_material`.

    Returns:
        (n_material, reasons). n_material counts:
          - any symbol where prior vs new is_material
          - any symbol present in prior but ABSENT from new (implicit flat)
          - any symbol present in new but ABSENT from prior (new commitment)

        `reasons` lists the per-symbol reason strings for observability.
    """
    prior_by_symbol = {(r.get("symbol") or "").upper(): r for r in prior_rows}
    new_by_symbol = {(c.get("symbol") or "").upper(): c for c in new_resolved}

    n_material = 0
    reasons: list[str] = []

    for sym, n in new_by_symbol.items():
        p = prior_by_symbol.get(sym)
        material, reason = is_material(p, n)
        if material:
            n_material += 1
            reasons.append(f"{sym}:{reason}")

    # Symbols held before but omitted now — implicit flat. Always material.
    for sym in prior_by_symbol.keys() - new_by_symbol.keys():
        n_material += 1
        reasons.append(f"{sym}:implicit_flat")

    return n_material, reasons
