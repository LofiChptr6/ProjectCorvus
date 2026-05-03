"""Thin compatibility shim — the real connection lives in ibkr/daemon.py.

Historically this module owned a singleton `ib_async.IB` connection. After the
daemon refactor (one connection lives in ibkr-daemon.service), the helpers in
this package talk to the daemon over HTTP via `ibkr._rpc`. The functions kept
here are the ones still imported elsewhere: `configure`, `get_mode`,
`disconnect`. `get_ib()` is gone — there is no in-process IB anymore.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

_config: dict = {}
_mode_cache: Optional[str] = None


def configure(cfg: dict) -> None:
    """Kept for caller compatibility (mcp_server, scheduled tasks). The daemon
    reads its own config from disk; we only stash it locally for `get_mode()`
    fallback when the daemon is unreachable during startup."""
    global _config
    _config = cfg


def get_mode() -> str:
    """Return 'paper' or 'live' from the locally-loaded config.

    The daemon validates port/mode symmetry at connect time, so as long as the
    daemon is up the value here is authoritative. Cached for process lifetime.
    """
    global _mode_cache
    if _mode_cache is not None:
        return _mode_cache
    mode = _config.get("trading", {}).get("mode", "paper")
    _mode_cache = mode
    return mode


async def disconnect() -> None:
    """No-op: the daemon owns the connection. Kept so legacy callers don't break."""
    return None
