"""Singleton async IBKR connection via ib_async.

Usage:
    from ibkr.client import get_ib
    ib = await get_ib()
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

_ib_instance = None
_config: dict = {}


def configure(cfg: dict) -> None:
    global _config
    _config = cfg


async def get_ib():
    """Return the shared IB instance, connecting if needed."""
    global _ib_instance
    if _ib_instance is not None and _ib_instance.isConnected():
        return _ib_instance
    return await _connect()


async def _connect(retries: int = 2, delay: float = 1.0, timeout: float = 8.0):
    global _ib_instance
    import random
    from ib_async import IB

    ibcfg = _config.get("ibkr", {})
    host = ibcfg.get("host", "127.0.0.1")
    port = ibcfg.get("port", 4002)
    # Three-tier clientId resolution:
    #   1. IBKR_CLIENT_ID env var (explicit caller intent — concierge=2, scheduled tasks=11-14)
    #   2. config.yaml default (only if no env — typical for the interactive MCP server, =1)
    #   3. If a "clientId already in use" error fires on tier 1/2, randomize in [50, 999] and retry.
    # The random tier exists because Claude Code's MCP launcher does NOT propagate parent env
    # to MCP subprocesses, so scheduled tasks would otherwise always collide on clientId 1.
    env_id = os.environ.get("IBKR_CLIENT_ID")
    base_client_id = int(env_id) if env_id else int(ibcfg.get("client_id", 1))
    expected_mode = _config.get("trading", {}).get("mode", "paper")

    ib = IB()
    candidate_ids: list[int] = [base_client_id]
    # Pre-roll random fallbacks so each retry has a fresh ID ready.
    candidate_ids.extend(random.sample(range(50, 1000), k=retries + 4))

    last_exc: Exception | None = None
    client_id = base_client_id
    for attempt, client_id in enumerate(candidate_ids, start=1):
        try:
            await asyncio.wait_for(
                ib.connectAsync(host, port, clientId=client_id),
                timeout=timeout,
            )
            if client_id != base_client_id:
                log.warning(
                    "IBKR clientId %s was busy — fell back to randomized clientId=%s",
                    base_client_id, client_id,
                )
            log.info("Connected to IBKR at %s:%s (clientId=%s)", host, port, client_id)
            break
        except asyncio.TimeoutError as exc:
            last_exc = exc
            log.warning("IBKR connect attempt %d (clientId=%s) timed out after %.1fs", attempt, client_id, timeout)
        except Exception as exc:
            last_exc = exc
            log.warning("IBKR connect attempt %d (clientId=%s) failed: %s", attempt, client_id, exc)
        # Disconnect any half-open socket before retrying
        try:
            if ib.isConnected():
                await ib.disconnectAsync()
        except Exception:
            pass
        if attempt >= retries + 1:  # +1 because first attempt uses the base, then `retries` randoms
            raise TimeoutError(
                f"IBKR gateway at {host}:{port} unresponsive across {attempt} attempts "
                f"(base clientId={base_client_id} + randomized fallbacks). Last error: {last_exc}"
            ) from last_exc
        await asyncio.sleep(delay)

    # Validate paper/live mode matches config
    actual_paper = port in (4002, 7497)
    expected_paper = expected_mode == "paper"
    if actual_paper != expected_paper:
        await ib.disconnectAsync()
        raise RuntimeError(
            f"Mode mismatch: config says '{expected_mode}' but port {port} is "
            f"{'paper' if actual_paper else 'live'}. Update config.yaml or connect to the right gateway."
        )

    log.info("Trading mode: %s", expected_mode)
    _ib_instance = ib
    ib.disconnectedEvent += _on_disconnect
    return ib


def _on_disconnect() -> None:
    global _ib_instance
    log.warning("IBKR disconnected")
    _ib_instance = None


async def disconnect() -> None:
    global _ib_instance
    if _ib_instance and _ib_instance.isConnected():
        await _ib_instance.disconnectAsync()
    _ib_instance = None


def get_mode() -> str:
    return _config.get("trading", {}).get("mode", "paper")
