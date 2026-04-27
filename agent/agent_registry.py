"""Loads and validates agent definitions from agents/*.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

_AGENTS_DIR = Path(__file__).parent.parent / "agents"
_cache: dict[str, dict] = {}


def load_agent(name: str) -> dict:
    if name in _cache:
        return _cache[name]
    path = _AGENTS_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Agent definition not found: {path}")
    with open(path) as f:
        cfg = yaml.safe_load(f)
    _validate(cfg)
    _cache[name] = cfg
    return cfg


def list_agents(enabled_only: bool = True) -> list[dict]:
    agents = []
    for path in sorted(_AGENTS_DIR.glob("*.yaml")):
        with open(path) as f:
            cfg = yaml.safe_load(f)
        # Skip non-agent files in agents/ (e.g. sector_map.yaml). An agent
        # config must declare a top-level `name`.
        if not isinstance(cfg, dict) or "name" not in cfg:
            continue
        if enabled_only and not cfg.get("enabled", True):
            continue
        agents.append(cfg)
    return agents


def _validate(cfg: dict) -> None:
    required = ["name", "system_prompt", "allocation_pct"]
    for field in required:
        if field not in cfg:
            raise ValueError(f"Agent definition missing required field: '{field}'")
    pct = cfg["allocation_pct"]
    if not (isinstance(pct, (int, float)) and 0.0 <= pct <= 1.0):
        raise ValueError(f"allocation_pct must be 0.0–1.0, got {pct!r}")
