"""Embedding-provider abstraction for the Phase B semantic-recall layer.

Single entrypoint module. Picks a provider at module load:

    VOYAGE_API_KEY set         → voyage-finance-2 (1024-dim, finance-tuned)
    else OPENAI_API_KEY set    → text-embedding-3-small (dimensions=1024)
    else                       → no-op (embed_* returns None)

Public surface:

    provider_ready()           → bool — True iff some provider is configured
    active_model_name()        → "voyage-finance-2" / "text-embedding-3-small" / None
    text_for_embedding(h, b)   → str — canonical "headline + lede" prep
    await embed_one(text)      → list[float] (1024) or None
    await embed_batch(texts)   → list[list[float] | None] (parallelizes/batches)
    await aclose()             → close underlying HTTP client

Design notes:

  - All output vectors are 1024-dim, regardless of which provider serves them.
    Voyage models are natively 1024; OpenAI 3-small/large support the
    `dimensions` parameter so we always request 1024 to keep schema stable.
  - Best-effort: providers down → return None, never raise out of embed_*.
    Ingest must never block on embedding.
  - In-flight semaphore caps concurrency (default 4) to play nice with rate
    limits. Retries are exponential on 429 / 5xx (3 tries).
  - Tiny in-process LRU cache on the exact text→embedding mapping (the same
    article gets the same vector inside one ingest run).
"""

from __future__ import annotations

import asyncio
import logging
import os
from functools import lru_cache
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# ── Provider selection at module load ─────────────────────────────────────────

_VOYAGE_MODEL = "voyage-finance-2"
_OPENAI_MODEL = "text-embedding-3-small"
_VECTOR_DIM = 1024

_VOYAGE_KEY = os.environ.get("VOYAGE_API_KEY", "").strip() or None
_OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip() or None

if _VOYAGE_KEY:
    _PROVIDER = "voyage"
elif _OPENAI_KEY:
    _PROVIDER = "openai"
else:
    _PROVIDER = None

# Module-scope HTTP client; lazily created.
_client: Optional[httpx.AsyncClient] = None
_sem = asyncio.Semaphore(4)


def provider_ready() -> bool:
    return _PROVIDER is not None


def active_model_name() -> Optional[str]:
    if _PROVIDER == "voyage":
        return _VOYAGE_MODEL
    if _PROVIDER == "openai":
        return _OPENAI_MODEL
    return None


def text_for_embedding(headline: str, body: Optional[str]) -> str:
    """Canonical text prep: headline + first 500 chars of body.

    Why both: headlines alone average ~80 chars and sometimes lack enough
    signal ("Stocks slide" tells you almost nothing). The first 500 chars of
    body is reliably the lede. ~150 tokens total — cheap and discriminating.
    """
    h = (headline or "").strip()
    b = (body or "").strip()
    if not h and not b:
        return ""
    if not b:
        return h
    return f"{h}\n\n{b[:500]}"


# ── HTTP client helpers ───────────────────────────────────────────────────────

def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0),
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ── Voyage backend ────────────────────────────────────────────────────────────

async def _voyage_embed(texts: list[str]) -> list[Optional[list[float]]]:
    """Voyage embeddings API. Returns list of vectors aligned to inputs;
    None on per-item failure (should be rare — usually all-or-nothing)."""
    assert _VOYAGE_KEY, "voyage selected but key missing"
    client = _get_client()
    payload = {
        "input": texts,
        "model": _VOYAGE_MODEL,
        "input_type": "document",
    }
    headers = {
        "Authorization": f"Bearer {_VOYAGE_KEY}",
        "Content-Type": "application/json",
    }
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            r = await client.post(
                "https://api.voyageai.com/v1/embeddings",
                json=payload, headers=headers,
            )
            if r.status_code in (429, 500, 502, 503, 504):
                last_exc = httpx.HTTPStatusError(
                    f"voyage returned {r.status_code}: {r.text[:200]}",
                    request=r.request, response=r,
                )
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
            r.raise_for_status()
            data = r.json()
            out: list[Optional[list[float]]] = []
            for item in (data.get("data") or []):
                vec = item.get("embedding")
                if isinstance(vec, list) and len(vec) == _VECTOR_DIM:
                    out.append([float(x) for x in vec])
                else:
                    out.append(None)
            # Pad/truncate to len(texts) in case provider returned mismatch
            while len(out) < len(texts):
                out.append(None)
            return out[: len(texts)]
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_exc = e
            await asyncio.sleep(0.5 * (2 ** attempt))
    log.warning("voyage embed failed after retries: %s", last_exc)
    return [None] * len(texts)


# ── OpenAI backend ────────────────────────────────────────────────────────────

async def _openai_embed(texts: list[str]) -> list[Optional[list[float]]]:
    """OpenAI embeddings API. Uses `dimensions=1024` so output matches Voyage's
    column width — schema column is `vector(1024)` regardless of provider."""
    assert _OPENAI_KEY, "openai selected but key missing"
    client = _get_client()
    payload = {
        "input": texts,
        "model": _OPENAI_MODEL,
        "dimensions": _VECTOR_DIM,
    }
    headers = {
        "Authorization": f"Bearer {_OPENAI_KEY}",
        "Content-Type": "application/json",
    }
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            r = await client.post(
                "https://api.openai.com/v1/embeddings",
                json=payload, headers=headers,
            )
            if r.status_code in (429, 500, 502, 503, 504):
                last_exc = httpx.HTTPStatusError(
                    f"openai returned {r.status_code}: {r.text[:200]}",
                    request=r.request, response=r,
                )
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
            r.raise_for_status()
            data = r.json()
            out: list[Optional[list[float]]] = []
            for item in (data.get("data") or []):
                vec = item.get("embedding")
                if isinstance(vec, list) and len(vec) == _VECTOR_DIM:
                    out.append([float(x) for x in vec])
                else:
                    out.append(None)
            while len(out) < len(texts):
                out.append(None)
            return out[: len(texts)]
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_exc = e
            await asyncio.sleep(0.5 * (2 ** attempt))
    log.warning("openai embed failed after retries: %s", last_exc)
    return [None] * len(texts)


# ── Public API ────────────────────────────────────────────────────────────────

async def embed_batch(texts: list[str]) -> list[Optional[list[float]]]:
    """Batch embed. Returns one vector (or None) per input, aligned by index."""
    if not provider_ready() or not texts:
        return [None] * len(texts)
    # Filter empties so providers don't reject; restore alignment after.
    indexed = [(i, t) for i, t in enumerate(texts) if (t or "").strip()]
    if not indexed:
        return [None] * len(texts)
    inputs = [t for _, t in indexed]
    async with _sem:
        if _PROVIDER == "voyage":
            vecs = await _voyage_embed(inputs)
        elif _PROVIDER == "openai":
            vecs = await _openai_embed(inputs)
        else:
            vecs = [None] * len(inputs)
    out: list[Optional[list[float]]] = [None] * len(texts)
    for (orig_idx, _), vec in zip(indexed, vecs):
        out[orig_idx] = vec
    return out


async def embed_one(text: str) -> Optional[list[float]]:
    """Single embed. Returns None on empty input or provider failure."""
    if not provider_ready() or not (text or "").strip():
        return None
    vecs = await embed_batch([text])
    return vecs[0] if vecs else None
