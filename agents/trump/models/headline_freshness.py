"""Trump bootstrap indicator: headline_freshness.

Doesn't actually use bars — Trump primarily trades news, not technicals. This
returns the % move on the latest bar so Trump can compare 'how much already moved
on the headline' vs 'how fresh is the headline'. Trump should pass the headline
timestamp via context to enrich the read.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if not bars:
        return {"signal": None, "reason": "no bars"}

    last = bars[-1]
    open_ = last.get("o") or 0
    close = last.get("c") or 0
    pct_today = (close - open_) / open_ * 100 if open_ else 0.0

    headline_ts = context.get("headline_timestamp")  # ISO string from caller
    age_min = None
    if headline_ts:
        try:
            t = datetime.fromisoformat(headline_ts.replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - t).total_seconds() / 60.0
        except Exception:
            pass

    priced_in = abs(pct_today) >= 1.0

    if pct_today > 1.5:
        direction = "long"
        e_return = min(pct_today * 0.7, 4.0)
    elif pct_today < -1.5:
        direction = "short"
        e_return = max(pct_today * 0.7, -4.0)
    else:
        direction = "flat"
        e_return = 0.0
    horizon = 3
    likelihood = round(abs(e_return) / horizon, 4) if horizon else 0.0

    return {
        "signal": round(pct_today, 3),
        "pct_move_today": round(pct_today, 3),
        "headline_age_minutes": age_min,
        "likely_priced_in": priced_in,
        "interpretation": (
            "easy money likely gone — already moved >=1%" if priced_in
            else "fresh tape — playbook still actionable"
        ),
        "direction": direction,
        "likelihood": likelihood,
        "expected_return_pct": round(e_return, 3),
        "time_to_target_days": horizon,
        "inputs": {
            "pct_today": round(pct_today, 3),
            "age_minutes": age_min,
        },
    }
