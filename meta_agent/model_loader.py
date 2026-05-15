"""Auto-discovery loader for per-agent quant models.

Each sector agent owns a directory `agents/{name}/models/` containing one or more
.py files. Each file is expected to expose:

    def compute(symbol: str, bars: list[dict], context: dict) -> dict:
        ...

Plus optionally:

    MODEL_VERSION = "X.Y"   # for tracking; "unset" if missing

Files without a `compute` function are skipped silently. Subdirectories are
ignored (use `models/scrapped/` to retire models without losing the audit trail).

The loader mirrors the importlib + reload pattern used by
`mcp_server.compute_custom_indicator` so model edits land without an MCP server
restart.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_MODEL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def discover_models(agent_name: str, agents_root: Path | None = None) -> list[str]:
    """Return sorted list of model module names (no .py extension, no __init__)
    in agents/{agent_name}/models/. Validates names against regex for safety.

    Args:
        agent_name: Sector agent name (e.g. 'atlas').
        agents_root: Root path to the `agents/` directory. Defaults to `Path("agents")`
                     (relative — caller is expected to cwd into the repo).
    """
    if not _AGENT_NAME_RE.match(agent_name or ""):
        return []
    root = agents_root if agents_root is not None else Path("agents")
    model_dir = root / agent_name / "models"
    if not model_dir.exists() or not model_dir.is_dir():
        return []
    out: list[str] = []
    for py in sorted(model_dir.glob("*.py")):
        name = py.stem
        if name == "__init__":
            continue
        if not _MODEL_NAME_RE.match(name):
            continue
        out.append(name)
    return out


def run_all_models(
    agent_name: str,
    symbol: str,
    bars: list[dict],
    context: dict[str, Any],
    agents_root: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Discover every model in agents/{agent_name}/models/ and invoke each
    with (symbol, bars, context).

    Returns a dict shaped like:
        {
            model_name: {
                "version": "X.Y" | "unset",
                "result": <whatever model.compute returns> | None,
                "error": "TypeError: ..." | None,
            },
            ...
        }

    Per-model failures are captured in the per-model dict — one bad model does
    NOT block others. Modules are reloaded each call so model edits land
    without restarting the MCP server.
    """
    out: dict[str, dict[str, Any]] = {}
    for name in discover_models(agent_name, agents_root=agents_root):
        entry: dict[str, Any] = {"version": "unset", "result": None, "error": None}
        try:
            module = importlib.import_module(f"agents.{agent_name}.models.{name}")
            module = importlib.reload(module)
            entry["version"] = getattr(module, "MODEL_VERSION", "unset")
            if not hasattr(module, "compute"):
                entry["error"] = "no compute(symbol, bars, context) entry point"
            else:
                # If the model declares EXTRA_SYMBOLS, ensure context["extra_bars"]
                # has a non-empty entry for each. Callers that already populate
                # extra_bars (the runtime helper) pass through unchanged; callers
                # that don't (the validator's registry build) get the main `bars`
                # arg as a synthetic stand-in so the model can still hit its
                # `inputs`-emitting code path.
                model_ctx = context
                extras = list(getattr(module, "EXTRA_SYMBOLS", []) or [])
                if extras:
                    existing = dict(context.get("extra_bars") or {})
                    filled = False
                    for sym in extras:
                        if not existing.get(sym):
                            existing[sym] = bars
                            filled = True
                    if filled:
                        model_ctx = {**context, "extra_bars": existing}
                entry["result"] = module.compute(symbol, bars, model_ctx)
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
        out[name] = entry
    return out
