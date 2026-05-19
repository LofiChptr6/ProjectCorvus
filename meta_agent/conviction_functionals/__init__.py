"""Pluggable conviction functionals.

A *conviction functional* maps a probabilistic forecast (or a weighted mixture
of them) plus the horizon to a scalar in [0, 1] that the allocator treats as
"confidence × magnitude" the same way it treated the legacy LLM-supplied
conviction. This lets multiple functionals — `μ·conc/√t`, fractional Kelly,
literal `peak(x·p)/t`, CVaR-weighted, info-ratio-of-edge — be A/B-compared on
the same stored beliefs.

Architecture (two-stage, per the plan):

  STAGE 1: summaries/    pure functions distribution → scalar feature
                         (mean, variance, concentration, peak, tail, cvar)

  STAGE 2: combiners/    declare which summaries they need + raw output range;
                         registry composes summaries → raw scalar → normalize
                         to [0,1] → clamp.

The allocator (and the replay harness) call run(name, distribution, t_days)
to get the final scalar. Switching the functional is a string change.

Per-agent selection (Phase G): each agent may declare a preferred functional
in agents/<agent>.yaml:

    conviction_functional: frac_kelly

When unset, falls back to DEFAULT_FUNCTIONAL. The runner reads this on every
model-backed conviction submission so per-agent A/B emerges naturally from
the data-driven leaderboard (scripts/suggest_functional_per_agent.py).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Iterable, Optional

from .combiners import expected_return, frac_kelly, peak_xp_over_t
from .summaries import concentration, cvar, mean, peak, tail, variance

log = logging.getLogger(__name__)

_AGENTS_ROOT = Path(__file__).resolve().parent.parent.parent / "agents"

# Summary registry: name -> callable(bins_xs, bins_ps) -> float.
# Combiners declare what they need by name.
SUMMARIES: dict[str, Callable[[list[float], list[float]], float]] = {
    "mean":          mean.compute,
    "variance":      variance.compute,
    "concentration": concentration.compute,
    "peak":          peak.compute,
    "tail_pos":      tail.compute_positive,
    "tail_neg":      tail.compute_negative,
    "cvar_95":       cvar.compute_95,
}


# Combiner registry: name -> (callable, required_summaries, raw_range)
# raw_range gives the registry the bounds it needs to normalize to [0, 1].
# A combiner returning values outside its declared range is clamped at the
# boundary before normalization (defensive).
COMBINERS: dict[str, dict] = {
    "expected_return": {
        "fn":         expected_return.compute,
        "summaries":  ("mean", "concentration"),
        "raw_range":  (-0.5, 0.5),  # μ in % units * concentration / √t_days
    },
    "frac_kelly": {
        "fn":         frac_kelly.compute,
        "summaries":  ("mean", "variance"),
        "raw_range":  (0.0, 1.0),   # already capped by the combiner
    },
    "peak_xp_over_t": {
        "fn":         peak_xp_over_t.compute,
        "summaries":  ("mean", "peak"),
        "raw_range":  (-1.0, 1.0),
    },
}

DEFAULT_FUNCTIONAL = "expected_return"


def list_functionals() -> list[str]:
    return sorted(COMBINERS.keys())


def _coerce_bins(distribution: dict) -> tuple[list[float], list[float]]:
    bins = distribution.get("bins") or []
    xs = [float(b["x"]) for b in bins]
    ps = [float(b["p"]) for b in bins]
    return xs, ps


def run(name: str, distribution: dict, t_days: float) -> float:
    """Apply named functional to a single distribution at a given horizon.

    Returns a scalar in [0, 1] suitable for the allocator's `conviction`
    field. Always non-negative — the sign (long vs short inverse-ETF) lives
    on `direction`, not in the magnitude.

    Raises KeyError on unknown name (caller's fault — registry is closed).
    """
    spec = COMBINERS[name]
    xs, ps = _coerce_bins(distribution)

    # Compute only the summaries the combiner needs.
    summary_values: dict[str, float] = {
        s: SUMMARIES[s](xs, ps) for s in spec["summaries"]
    }
    raw = spec["fn"](summaries=summary_values, t_days=float(t_days))

    lo, hi = spec["raw_range"]
    # Use |raw| because the magnitude is what we normalize; sign is encoded
    # separately. Clamp first, then min/max-normalize to [0, 1].
    mag = abs(raw)
    span = max(abs(lo), abs(hi))
    if span == 0:
        return 0.0
    mag = min(mag, span)
    return mag / span


def functional_for_agent(agent_name: str) -> str:
    """Return the conviction-functional name the agent prefers, falling back
    to DEFAULT_FUNCTIONAL when:
      - the agent's YAML is missing,
      - the YAML does not declare `conviction_functional`,
      - or the declared name isn't a registered combiner.

    Reads agents/<agent>.yaml — same file shape mike-allocator + bundlers
    already load. Lazy-imports yaml so the registry stays import-cheap.
    """
    yaml_path = _AGENTS_ROOT / f"{agent_name}.yaml"
    if not yaml_path.exists():
        return DEFAULT_FUNCTIONAL
    try:
        import yaml as _yaml
        with yaml_path.open(encoding="utf-8") as f:
            data = _yaml.safe_load(f) or {}
    except Exception as exc:
        log.warning("functional_for_agent: failed to load %s: %s: %s",
                    yaml_path, type(exc).__name__, exc)
        return DEFAULT_FUNCTIONAL
    name = (data or {}).get("conviction_functional")
    if not name or not isinstance(name, str):
        return DEFAULT_FUNCTIONAL
    if name not in COMBINERS:
        log.warning("functional_for_agent: %s declared unknown functional %r; "
                    "falling back to %s", agent_name, name, DEFAULT_FUNCTIONAL)
        return DEFAULT_FUNCTIONAL
    return name


def collapse_across_horizons(
    name: str,
    horizon_distributions: Iterable[tuple[dict, float]],
) -> float:
    """Combine per-horizon scalars into one using 1/√t weighting (Sharpe
    scaling — short horizons should get more weight on a per-day basis).

    horizon_distributions: iterable of (distribution_dict, t_days) tuples.

    Returns a scalar in [0, 1]. Empty input → 0.
    """
    scalars_weights: list[tuple[float, float]] = []
    for dist, t in horizon_distributions:
        if t <= 0:
            continue
        c = run(name, dist, t)
        w = 1.0 / (t ** 0.5)
        scalars_weights.append((c, w))
    if not scalars_weights:
        return 0.0
    total_w = sum(w for _, w in scalars_weights)
    if total_w == 0:
        return 0.0
    return sum(c * w for c, w in scalars_weights) / total_w
