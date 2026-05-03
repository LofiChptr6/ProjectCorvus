"""Internal HTTP client for the IBKR daemon.

Every wrapper module under ibkr/ that used to hold an `ib_async.IB` connection
now calls into the daemon over localhost HTTP via this module. Keeps the
public function signatures elsewhere unchanged — only the body changes.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

_DEFAULT_URL = "http://127.0.0.1:7790"
_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)
_client: Optional[httpx.AsyncClient] = None


class DaemonError(RuntimeError):
    """Raised when the daemon returns a non-2xx response or can't be reached."""


class DaemonUnavailable(DaemonError):
    """Raised when the daemon is reachable but IBKR is not (HTTP 503)."""


def _load_env_once() -> None:
    """Load .env from project root if not already loaded. Safe to call repeatedly."""
    if os.environ.get("IBKR_DAEMON_TOKEN"):
        return
    try:
        from dotenv import load_dotenv
        root = Path(__file__).resolve().parent.parent
        load_dotenv(root / ".env")
    except ImportError:
        pass


def _base_url() -> str:
    return os.environ.get("IBKR_DAEMON_URL", _DEFAULT_URL).rstrip("/")


def _token() -> str:
    _load_env_once()
    tok = os.environ.get("IBKR_DAEMON_TOKEN", "")
    if not tok:
        raise DaemonError(
            "IBKR_DAEMON_TOKEN not set. Add it to .env (32 hex chars). "
            "The daemon refuses unauthenticated requests."
        )
    return tok


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=_base_url(),
            timeout=_TIMEOUT,
            headers={"Authorization": f"Bearer {_token()}"},
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code == 503:
        try:
            body = resp.json()
        except Exception:
            body = {"error": "ibkr_disconnected"}
        raise DaemonUnavailable(body.get("error", "ibkr_disconnected"))
    if resp.status_code >= 400:
        try:
            body = resp.json()
            msg = body.get("error") or body.get("detail") or resp.text
        except Exception:
            msg = resp.text
        raise DaemonError(f"daemon {resp.status_code}: {msg}")


async def get(path: str, params: Optional[dict] = None) -> Any:
    client = await _get_client()
    resp = await client.get(path, params=params)
    _raise_for(resp)
    return resp.json()


async def post(path: str, json: Optional[dict] = None) -> Any:
    client = await _get_client()
    resp = await client.post(path, json=json or {})
    _raise_for(resp)
    return resp.json()
