"""AsyncOpenAI client factory for the pipelines.

Routes through obs/proxy.py at :8001 by default (so audit_log + tool_calls get
captured for the Streamlit dashboard). Falls back to vLLM directly at :8000 if
the proxy healthz fails — observability is non-critical to execution.

Mirrors the URL logic in scripts/run_scheduled_skill.sh:55-61 so harness path
and pipeline path land in the same audit stream.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

import httpx
from openai import AsyncOpenAI


PROXY_BASE_DEFAULT = "http://localhost:8001"
VLLM_BASE_DEFAULT = "http://localhost:8000"
PROXY_HEALTHZ_TIMEOUT_SEC = 1.0


@dataclass
class LLMClient:
    client: AsyncOpenAI
    base_url: str
    session_id: str
    model: str


def _resolve_base_url(skill_name: str, session_id: str) -> str:
    """Return the OpenAI-compatible base URL the new client should hit.

    Preference: obs proxy (URL-tagged with /skill/<name>/session/<id>/v1) so
    audit logging works. Probe `/healthz` synchronously via httpx with a 1s
    cap; on failure, fall back to vLLM direct.
    """
    proxy_base = os.environ.get("LLM_PROXY_URL", PROXY_BASE_DEFAULT).rstrip("/")
    vllm_fallback = os.environ.get("LOCAL_LLM_FALLBACK_URL", VLLM_BASE_DEFAULT).rstrip("/")

    try:
        with httpx.Client(timeout=PROXY_HEALTHZ_TIMEOUT_SEC) as h:
            resp = h.get(f"{proxy_base}/healthz")
            if resp.status_code == 200:
                return f"{proxy_base}/skill/{skill_name}/session/{session_id}/v1"
    except Exception:
        pass
    return f"{vllm_fallback}/v1"


def make_client(
    skill_name: str,
    *,
    session_id: str | None = None,
    model: str | None = None,
) -> LLMClient:
    sid = session_id or str(uuid.uuid4())
    chosen_model = (
        model
        or os.environ.get("PIPELINE_MODEL")
        or os.environ.get("LOCAL_MODEL")
        or "Qwen/Qwen3-32B-FP8"
    )
    base_url = _resolve_base_url(skill_name, sid)
    api_key = os.environ.get("LOCAL_LLM_API_KEY", "local-dummy")
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    return LLMClient(client=client, base_url=base_url, session_id=sid, model=chosen_model)
