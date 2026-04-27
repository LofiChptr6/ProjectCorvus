#!/usr/bin/env python3
"""IBKR preflight check + orphan MCP cleanup.

Run before scheduled skills. Verifies the assigned IBKR clientId can connect.
If not, hunts for orphaned mcp_server processes (parent dead) holding the
slot, kills them, and retries once.

Exit codes:
    0 — gateway reachable on assigned clientId (after cleanup if needed)
    1 — gateway unreachable; skill should still launch and surface the error

NEVER kills:
    - concierge.service (long-running, owns clientId 2)
    - IB Gateway itself
    - MCP servers whose parent is alive (claude.exe, cmd.exe, pwsh.exe, etc.)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import psutil
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [preflight] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Parents that indicate a live, legitimate MCP server. If the actual parent
# matches one of these (case-insensitive), we leave the MCP alone.
LIVE_PARENT_NAMES = {
    "claude.exe",
    "cmd.exe",
    "pwsh.exe",
    "powershell.exe",
    "wt.exe",          # Windows Terminal
    "code.exe",        # VS Code
    "explorer.exe",    # double-clicked launcher
}


def _is_mcp_server(proc: psutil.Process) -> bool:
    """True if proc looks like our trading MCP server (NOT concierge)."""
    try:
        name = proc.name().lower()
        if name not in ("python.exe", "pythonw.exe"):
            return False
        cmdline = " ".join(proc.cmdline()).lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

    if "mcp_server" not in cmdline:
        return False
    # Defensive: never touch the concierge even if naming changes.
    if "concierge" in cmdline:
        return False
    return True


def _is_orphan(proc: psutil.Process) -> tuple[bool, str]:
    """Return (is_orphan, reason). PID-recycling-aware on Windows."""
    try:
        parent = proc.parent()
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        return True, f"parent lookup failed: {exc}"

    if parent is None:
        return True, "parent is None"

    try:
        parent_name = parent.name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True, "parent vanished"

    # PID-recycling defense: if the "parent" is something like System Idle (PID 0)
    # or a totally unrelated process, treat as orphan.
    if parent_name in LIVE_PARENT_NAMES:
        return False, f"parent alive ({parent_name})"

    # Created BEFORE child — required for a real parent. If parent.create_time
    # > child.create_time, it's a recycled PID.
    try:
        if parent.create_time() > proc.create_time():
            return True, f"parent PID recycled (parent={parent_name})"
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True, "parent timing check failed"

    # Unknown parent name — be conservative and DO NOT kill. Log it so we can
    # add it to LIVE_PARENT_NAMES if it turns out to be legitimate.
    return False, f"unknown parent ({parent_name}) — leaving alone"


def cleanup_orphans() -> int:
    """Kill orphaned mcp_server processes. Returns count killed."""
    killed = 0
    self_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "name"]):
        if proc.pid == self_pid:
            continue
        if not _is_mcp_server(proc):
            continue

        is_orphan, reason = _is_orphan(proc)
        if not is_orphan:
            log.info("Skipping pid=%s — %s", proc.pid, reason)
            continue

        try:
            log.warning("Killing orphan mcp_server pid=%s (%s)", proc.pid, reason)
            proc.kill()
            proc.wait(timeout=3)
            killed += 1
        except psutil.NoSuchProcess:
            killed += 1  # already gone, good
        except Exception as exc:
            log.error("Failed to kill pid=%s: %s", proc.pid, exc)
    return killed


async def try_connect(host: str, port: int, client_id: int, timeout: float = 5.0) -> bool:
    """Attempt a one-shot connection. Returns True on success."""
    from ib_async import IB

    ib = IB()
    try:
        await asyncio.wait_for(
            ib.connectAsync(host, port, clientId=client_id),
            timeout=timeout,
        )
        log.info("✓ Connected to IBKR %s:%s (clientId=%s)", host, port, client_id)
        return True
    except Exception as exc:
        log.warning("✗ Connect failed: %s", exc)
        return False
    finally:
        if ib.isConnected():
            try:
                await ib.disconnectAsync()
            except Exception:
                pass


async def main() -> int:
    config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    ibcfg = cfg.get("ibkr", {})
    host = ibcfg.get("host", "127.0.0.1")
    port = ibcfg.get("port", 4002)
    client_id = int(os.environ.get("IBKR_CLIENT_ID", ibcfg.get("client_id", 1)))

    log.info("Preflight: clientId=%s host=%s:%s", client_id, host, port)

    if await try_connect(host, port, client_id):
        return 0

    log.warning("First connect failed — scanning for orphan mcp_server processes")
    killed = cleanup_orphans()
    if killed == 0:
        log.warning("No orphans found. Gateway may be down or another live process holds the slot.")
        return 1

    log.info("Killed %d orphan(s). Sleeping 3s before retry.", killed)
    await asyncio.sleep(3)

    if await try_connect(host, port, client_id):
        log.info("✓ Recovered after orphan cleanup")
        return 0

    log.error("Still cannot connect after cleanup. Skill will launch and report the error.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
