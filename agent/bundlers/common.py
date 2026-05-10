"""Shared loaders for bundlers — workspace files, journal, sector config."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def read_workspace(agent_name: str) -> dict[str, Any]:
    """Mirror of mcp_server.read_my_workspace — read agents/<agent>/{notes,watchlist.md,data}."""
    base = Path("agents") / agent_name
    out: dict[str, Any] = {
        "agent_name": agent_name,
        "notes": [],
        "watchlist": "",
        "data": [],
    }
    if not base.is_dir():
        return out

    notes_dir = base / "notes"
    data_dir = base / "data"
    wl = base / "watchlist.md"

    if notes_dir.is_dir():
        for f in sorted(notes_dir.iterdir()):
            if not f.is_file() or f.suffix not in {".md", ".txt"}:
                continue
            try:
                body = f.read_text(encoding="utf-8")
            except Exception as e:
                body = f"(failed to read: {type(e).__name__}: {e})"
            out["notes"].append({
                "filename": f.name,
                "size": f.stat().st_size,
                "mtime": f.stat().st_mtime,
                "content": body[:8000],
            })
    if wl.is_file():
        try:
            out["watchlist"] = wl.read_text(encoding="utf-8")[:20000]
        except Exception as e:
            out["watchlist"] = f"(failed to read watchlist.md: {type(e).__name__}: {e})"
    if data_dir.is_dir():
        for f in sorted(data_dir.iterdir()):
            if not f.is_file():
                continue
            out["data"].append({
                "filename": f.name,
                "size": f.stat().st_size,
                "mtime": f.stat().st_mtime,
            })
    return out


async def load_journal_split(agent_name: str) -> dict[str, list[dict]]:
    """Mirror of mcp_server.get_my_journal — open + due + recent_resolutions."""
    from datetime import date as _date
    from db import store
    today = _date.today().isoformat()
    return {
        "open": await store.get_open_theses(agent_name, limit=10),
        "due_today_or_earlier": await store.get_theses_due(agent_name, on_or_before=today),
        "recent_resolutions": await store.get_recent_resolutions(agent_name, limit=3),
    }
