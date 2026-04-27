"""Collects all TOOL_DEFs and execute() functions for the Claude API."""

from __future__ import annotations

import importlib
from typing import Callable

_TOOL_MODULES = [
    "tools.market.get_quote",
    "tools.market.get_bars",
    "tools.market.run_scanner",
    "tools.market.get_news",
    "tools.account.get_positions",
    "tools.account.get_balances",
    "tools.account.get_open_orders",
    "tools.execution.place_order",
    "tools.execution.cancel_order",
    "tools.execution.modify_order",
    "tools.analysis.get_pnl_summary",
    "tools.analysis.get_trade_blotter",
    "tools.analysis.compute_technicals",
]

_registry: dict[str, tuple[dict, Callable]] = {}


def _load() -> None:
    for module_path in _TOOL_MODULES:
        mod = importlib.import_module(module_path)
        name = mod.TOOL_DEF["name"]
        _registry[name] = (mod.TOOL_DEF, mod.execute)


def get_all_tools() -> list[dict]:
    if not _registry:
        _load()
    return [tool_def for tool_def, _ in _registry.values()]


def get_executor(tool_name: str) -> Callable:
    if not _registry:
        _load()
    entry = _registry.get(tool_name)
    if not entry:
        raise KeyError(f"Unknown tool: {tool_name}")
    return entry[1]
