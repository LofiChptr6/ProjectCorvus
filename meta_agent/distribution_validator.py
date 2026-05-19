"""Schema validator for the probabilistic-forecast distribution payload.

A distribution is a discrete probability over expected return at a given
horizon. Shape (closed — `extras` would rot into a junk drawer):

    {
        "anchor_price": 195.50,
        "anchor_ts":    "2026-05-16T17:30:00Z",   # UTC ISO-8601
        "axis":         "return_pct",              # or "log_return"
        "horizon":      "5m",                      # 5m | 1h | 1d | 1w
        "bins": [
            {"x": -2.0, "p": 0.05},
            {"x": -1.0, "p": 0.20},
            {"x":  0.0, "p": 0.50},
            {"x":  1.0, "p": 0.20},
            {"x":  2.0, "p": 0.05}
        ],
        "model":         "ou_mean_revert",
        "model_version": "0.2.0"
    }

Rules (all enforced server-side at submit time):
  - bins is a list of dicts with 3 ≤ n ≤ 20 entries
  - each p ≥ MIN_P (1e-4) — additive smoothing floor; prevents -inf log-loss
    on the realized bin if the model puts zero mass there.
  - sum(p) ≈ 1 within SUM_TOLERANCE (1e-4)
  - x strictly increasing
  - uniform x spacing (max relative deviation < UNIFORM_SPACING_TOLERANCE)
    required so CRPS via empirical step-CDF is well-defined and comparable
    across models.
  - horizon ∈ ALLOWED_HORIZONS
  - axis ∈ ALLOWED_AXES
  - model / model_version are non-empty strings
  - anchor_price > 0; anchor_ts parses as ISO-8601 UTC

Used at:
  - db.store.upsert_forecasts_batch (every batch submit)
  - meta_agent.conviction_from_model (model-emitted distributions)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

ALLOWED_HORIZONS = {"5m", "1h", "1d", "1w"}
ALLOWED_AXES = {"return_pct", "log_return"}

MIN_P = 1e-4
SUM_TOLERANCE = 1e-4
UNIFORM_SPACING_TOLERANCE = 1e-6
MIN_BINS = 3
MAX_BINS = 20


def validate_distribution(d: Any) -> tuple[bool, Optional[str]]:
    """Return (True, None) on a valid distribution payload, (False, reason)
    otherwise. Reason is a short human-readable string suitable for the
    submit-time error response or a validator log line."""
    if not isinstance(d, dict):
        return False, f"distribution must be a dict, got {type(d).__name__}"

    for key in ("anchor_price", "anchor_ts", "axis", "horizon", "bins", "model", "model_version"):
        if key not in d:
            return False, f"missing required key: {key!r}"

    try:
        anchor_price = float(d["anchor_price"])
    except (TypeError, ValueError):
        return False, f"anchor_price must be numeric, got {d['anchor_price']!r}"
    if anchor_price <= 0:
        return False, f"anchor_price must be > 0, got {anchor_price}"

    if not isinstance(d["anchor_ts"], str):
        return False, "anchor_ts must be an ISO-8601 string"
    ts_str = d["anchor_ts"]
    try:
        datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return False, f"anchor_ts not ISO-8601 parseable: {ts_str!r}"

    axis = d.get("axis")
    if axis not in ALLOWED_AXES:
        return False, f"axis must be one of {sorted(ALLOWED_AXES)}, got {axis!r}"

    horizon = d.get("horizon")
    if horizon not in ALLOWED_HORIZONS:
        return False, f"horizon must be one of {sorted(ALLOWED_HORIZONS)}, got {horizon!r}"

    model = d.get("model")
    if not isinstance(model, str) or not model.strip():
        return False, "model must be a non-empty string"
    model_version = d.get("model_version")
    if not isinstance(model_version, str) or not model_version.strip():
        return False, "model_version must be a non-empty string"

    bins = d.get("bins")
    if not isinstance(bins, list):
        return False, "bins must be a list"
    n = len(bins)
    if n < MIN_BINS or n > MAX_BINS:
        return False, f"bins length must be in [{MIN_BINS},{MAX_BINS}], got {n}"

    xs: list[float] = []
    ps: list[float] = []
    for i, b in enumerate(bins):
        if not isinstance(b, dict) or "x" not in b or "p" not in b:
            return False, f"bin[{i}] must be a dict with keys 'x' and 'p'"
        try:
            xi = float(b["x"])
            pi = float(b["p"])
        except (TypeError, ValueError):
            return False, f"bin[{i}] x/p must be numeric"
        if pi < MIN_P:
            return False, f"bin[{i}] p={pi} below smoothing floor {MIN_P}"
        xs.append(xi)
        ps.append(pi)

    # Strictly increasing
    for i in range(1, n):
        if xs[i] <= xs[i - 1]:
            return False, f"bins x must be strictly increasing; xs[{i-1}]={xs[i-1]} xs[{i}]={xs[i]}"

    # Uniform spacing
    spacing = xs[1] - xs[0]
    if spacing <= 0:
        return False, "uniform spacing must be > 0"
    for i in range(2, n):
        d_i = xs[i] - xs[i - 1]
        rel = abs(d_i - spacing) / abs(spacing)
        if rel > UNIFORM_SPACING_TOLERANCE:
            return False, (
                f"non-uniform spacing at bin[{i}]: expected {spacing}, got {d_i} "
                f"(rel dev {rel:.2e} > {UNIFORM_SPACING_TOLERANCE})"
            )

    # Probabilities sum to 1
    p_sum = sum(ps)
    if abs(p_sum - 1.0) > SUM_TOLERANCE:
        return False, f"sum(p)={p_sum} not within tolerance {SUM_TOLERANCE} of 1.0"

    return True, None


def horizon_to_ttd_days(horizon: str) -> int:
    """Map distribution.horizon to time_to_target_days INTEGER (the column is
    integer-typed). Sub-daily horizons get 1; weekly = 7. The exact horizon
    name is preserved on `agent_forecast.horizon` and inside
    `distribution.horizon` so the resolver can resolve at the right boundary."""
    return {"5m": 1, "1h": 1, "1d": 1, "1w": 7}.get(horizon, 1)


def horizon_to_minutes(horizon: str) -> int:
    """Effective horizon length in minutes. Used by the resolver-scorer and by
    the allocator's anchor-staleness gate (skip distribution if
    now-anchor_ts > 0.3 * horizon_minutes)."""
    return {"5m": 5, "1h": 60, "1d": 60 * 24, "1w": 60 * 24 * 7}.get(horizon, 60 * 24)
