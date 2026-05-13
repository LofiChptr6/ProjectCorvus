"""Validate `agent_conviction.model_inputs` jsonb against what each agent's
quant models actually emit.

Without this, agents drift into LLM-fabricated technical-indicator blobs that
look plausible but never came out of `agents/<name>/models/`. The hourly audit
on 2026-05-12 found 23/34 recent convictions had fabricated keys (RSI_14,
BBANDS_20, …) and 0 had real-model keys (score, z, above_sma200, …).

How it works:
- On first use, introspect every model under agents/*/models/ by calling its
  compute(symbol, bars, context) with synthetic OHLCV bars, and harvest the
  keys of result['inputs'] (the canonical replay payload).
- Registry maps agent_name → frozenset of allowed model_inputs keys.
- validate(agent, model_inputs) returns (True, None) on pass, (False, reason)
  on a fabrication.
- Caller decides whether to log + accept (warn-only) or reject. See
  MODEL_INPUTS_VALIDATOR_MODE env var.
"""
from __future__ import annotations

import logging
import os
import random
import threading
from pathlib import Path
from typing import Optional

from meta_agent import model_loader

log = logging.getLogger("model_inputs_validator")

_AGENTS_ROOT = Path(__file__).resolve().parent.parent / "agents"
_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "model_inputs_validator.log"
_REGISTRY_LOCK = threading.Lock()
_REGISTRY: Optional[dict[str, frozenset[str]]] = None
_HANDLER_ATTACHED = False


def _ensure_handler() -> None:
    global _HANDLER_ATTACHED
    if _HANDLER_ATTACHED:
        return
    _LOG_PATH.parent.mkdir(exist_ok=True)
    handler = logging.FileHandler(_LOG_PATH)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    _HANDLER_ATTACHED = True


def _synthetic_bars(n: int = 260) -> list[dict]:
    """OHLCV bars sufficient for any model's bar-count precondition (atlas
    regime_score needs >=200 daily bars; volt rate_duration ~60)."""
    rnd = random.Random(42)
    bars: list[dict] = []
    price = 100.0
    for _ in range(n):
        o = price
        c = price * (1 + (rnd.random() - 0.5) * 0.02)
        h = max(o, c) * (1 + rnd.random() * 0.01)
        lo = min(o, c) * (1 - rnd.random() * 0.01)
        v = 1_000_000 + int(rnd.random() * 5_000_000)
        bars.append({"o": o, "h": h, "l": lo, "c": c, "v": v})
        price = c
    return bars


def _extract_inputs_keys(result: object) -> set[str]:
    if not isinstance(result, dict):
        return set()
    inputs = result.get("inputs")
    if isinstance(inputs, dict):
        return set(inputs.keys())
    return set()


def _build_registry() -> dict[str, frozenset[str]]:
    out: dict[str, frozenset[str]] = {}
    if not _AGENTS_ROOT.exists():
        return out
    bars = _synthetic_bars()
    for agent_dir in sorted(_AGENTS_ROOT.iterdir()):
        if not agent_dir.is_dir():
            continue
        agent = agent_dir.name
        if not (agent_dir / "models").exists():
            continue
        per_model = model_loader.run_all_models(
            agent, "SPY", bars, {"regime": "MIXED"}, agents_root=_AGENTS_ROOT,
        )
        keys: set[str] = set()
        for name, entry in per_model.items():
            if entry.get("error"):
                log.warning("registry: agent=%s model=%s skipped (%s)", agent, name, entry["error"])
                continue
            keys |= _extract_inputs_keys(entry.get("result"))
        out[agent] = frozenset(keys)
        log.info("registry: agent=%s allowed=%s", agent, sorted(keys) or "(none)")
    return out


def get_registry() -> dict[str, frozenset[str]]:
    global _REGISTRY
    _ensure_handler()
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = _build_registry()
        return _REGISTRY


def reset_registry_for_tests() -> None:
    """Force rebuild on next get_registry(). Only for tests."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = None


def validate(
    agent_name: str,
    model_inputs: Optional[dict],
    *,
    symbol: str = "",
    direction: str = "",
) -> tuple[bool, Optional[str]]:
    """Return (True, None) when model_inputs is empty OR every top-level key
    is in the agent's allowed set. Return (False, reason) when at least one
    key did not come from any of the agent's quant models.

    On failure, emits a structured WARNING through this module's logger so
    the dedicated logs/model_inputs_validator.log file captures every hit
    regardless of which caller invoked the validator. `symbol` and
    `direction` are optional context for the log line."""
    if not model_inputs:
        return True, None
    if not isinstance(model_inputs, dict):
        reason = "model_inputs must be a dict"
        log.warning("agent=%s symbol=%s direction=%s reason=%s",
                    agent_name, symbol, direction, reason)
        return False, reason
    registry = get_registry()
    allowed = registry.get(agent_name)
    if allowed is None:
        reason = f"agent '{agent_name}' has no models under agents/{agent_name}/models/ — model_inputs must be empty"
        log.warning("agent=%s symbol=%s direction=%s reason=%s",
                    agent_name, symbol, direction, reason)
        return False, reason
    submitted = set(model_inputs.keys())
    fabricated = sorted(submitted - allowed)
    if fabricated:
        reason = (
            f"keys not produced by any {agent_name} model: {fabricated}; "
            f"allowed: {sorted(allowed) or '(none)'}"
        )
        log.warning("agent=%s symbol=%s direction=%s reason=%s",
                    agent_name, symbol, direction, reason)
        return False, reason
    return True, None


def is_reject_mode() -> bool:
    """Hard-reject bad submissions when MODEL_INPUTS_VALIDATOR_MODE=reject.
    Default is warn-only (log to logs/model_inputs_validator.log, accept)."""
    return os.environ.get("MODEL_INPUTS_VALIDATOR_MODE", "warn").lower() == "reject"
