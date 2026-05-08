"""Anthropic-shape /v1/messages proxy that captures every exchange to Postgres
and fans out streaming chunks to dashboard subscribers.

Sits between the `claude` CLI (which dials ANTHROPIC_BASE_URL) and the local
vLLM server (which serves /v1/messages natively). The CLI sees a transparent
forwarder; the dashboard sees:

  - audit_log rows (one per /v1/messages exchange, keyed by session_id)
  - tool_calls rows (one per tool_use block in the assistant turn)
  - live SSE on /stream/{session_id} for the currently-running tile

Run with:
    .venv/bin/uvicorn obs.proxy:app --host 127.0.0.1 --port 8001 --workers 1
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sse_starlette.sse import EventSourceResponse

from db import store

log = logging.getLogger("obs.proxy")
logging.basicConfig(
    level=os.environ.get("OBS_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

VLLM_URL = os.environ.get("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1").rstrip("/")
# We forward to /v1/messages on vLLM regardless of the trailing /v1 in LOCAL_LLM_BASE_URL.
VLLM_ROOT = VLLM_URL.removesuffix("/v1")

REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=900.0, write=10.0, pool=5.0)


# ── Agent name derivation ─────────────────────────────────────────────────────
# Most skills are <agent>-<routine>: atlas-review, fab-evening, vera-model-tune.
# A few don't follow that shape — encode them explicitly.
SKILL_TO_AGENT_OVERRIDES: dict[str, str] = {
    "hourly-review": "desk",
    "sector-archivist": "archivist",
    "mike-allocator": "mike",
    "mike-morning": "mike",
    "mike-midday": "mike",
    "cassidy-evening": "cassidy",
    "strategy-investigate": "investigate",
    "adhoc": "adhoc",
}


def derive_agent(skill: str) -> str:
    if skill in SKILL_TO_AGENT_OVERRIDES:
        return SKILL_TO_AGENT_OVERRIDES[skill]
    head = skill.split("-", 1)[0]
    return head or "unknown"


# ── In-memory state ───────────────────────────────────────────────────────────
# Single-worker uvicorn — these dicts live in one process. If we ever scale to
# multiple workers, switch to Redis pubsub + Postgres for live state.

LIVE_SESSIONS: dict[str, dict[str, Any]] = {}

SESSION_REQUEST_INDEX: dict[str, int] = defaultdict(int)


class PubSub:
    """Per-session-id fan-out. Subscribers get an asyncio.Queue of bytes chunks."""

    def __init__(self) -> None:
        self._channels: dict[str, list[asyncio.Queue[bytes | None]]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def subscribe(self, session_id: str) -> asyncio.Queue[bytes | None]:
        q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=2048)
        async with self._lock:
            self._channels[session_id].append(q)
        return q

    async def unsubscribe(self, session_id: str, q: asyncio.Queue[bytes | None]) -> None:
        async with self._lock:
            try:
                self._channels[session_id].remove(q)
            except ValueError:
                pass
            if not self._channels[session_id]:
                self._channels.pop(session_id, None)

    def publish(self, session_id: str, chunk: bytes) -> None:
        # Non-async fast path — drop chunks if a subscriber is too slow rather than block.
        for q in self._channels.get(session_id, ()):
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                log.warning("subscriber queue full for %s; dropping chunk", session_id)

    def close(self, session_id: str) -> None:
        for q in self._channels.get(session_id, ()):
            try:
                q.put_nowait(None)  # sentinel: stream ended
            except asyncio.QueueFull:
                pass


PUBSUB = PubSub()


# ── SSE stream accumulator ────────────────────────────────────────────────────
# Parses Anthropic SSE events to reconstruct the assistant turn for audit_log.

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


class StreamAccumulator:
    """Reassembles the assistant content array from streamed SSE events."""

    def __init__(self) -> None:
        self.message_id: str | None = None
        self.model: str | None = None
        self.stop_reason: str | None = None
        # Per content-block-index: {"type": "text"|"tool_use", "text": "...", or "name"+"input"+"id"+"input_buf"}
        self.blocks: dict[int, dict[str, Any]] = {}
        self.usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

    def consume(self, raw_event: bytes) -> None:
        """Feed one SSE event (event:... + data:... lines)."""
        try:
            text = raw_event.decode("utf-8", errors="replace")
        except Exception:
            return
        event_name = None
        data_str = None
        for line in text.splitlines():
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_str = line[5:].strip()
        if not event_name or not data_str:
            return
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            return
        if event_name == "message_start":
            msg = data.get("message", {})
            self.message_id = msg.get("id")
            self.model = msg.get("model")
            usage = msg.get("usage") or {}
            self.usage["input_tokens"] = usage.get("input_tokens", 0)
            self.usage["output_tokens"] = usage.get("output_tokens", 0)
        elif event_name == "content_block_start":
            idx = data.get("index", 0)
            cb = data.get("content_block", {})
            btype = cb.get("type", "text")
            if btype == "text":
                self.blocks[idx] = {"type": "text", "text": cb.get("text", "")}
            elif btype == "tool_use":
                self.blocks[idx] = {
                    "type": "tool_use",
                    "id": cb.get("id"),
                    "name": cb.get("name"),
                    "input_buf": "",
                    "input": cb.get("input") or {},
                }
        elif event_name == "content_block_delta":
            idx = data.get("index", 0)
            block = self.blocks.get(idx)
            if block is None:
                return
            delta = data.get("delta", {})
            dtype = delta.get("type")
            if dtype == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif dtype == "input_json_delta":
                block["input_buf"] = block.get("input_buf", "") + delta.get("partial_json", "")
        elif event_name == "content_block_stop":
            idx = data.get("index", 0)
            block = self.blocks.get(idx)
            if block and block.get("type") == "tool_use" and block.get("input_buf"):
                try:
                    block["input"] = json.loads(block["input_buf"])
                except json.JSONDecodeError:
                    log.warning("bad tool input JSON for block %d", idx)
        elif event_name == "message_delta":
            delta = data.get("delta", {})
            self.stop_reason = delta.get("stop_reason") or self.stop_reason
            usage = data.get("usage") or {}
            if "output_tokens" in usage:
                self.usage["output_tokens"] = usage["output_tokens"]

    def assistant_content(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for idx in sorted(self.blocks.keys()):
            block = self.blocks[idx]
            if block["type"] == "text":
                out.append({"type": "text", "text": block.get("text", "")})
            elif block["type"] == "tool_use":
                out.append({
                    "type": "tool_use",
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": block.get("input") or {},
                })
        return out

    def final_text(self) -> str:
        for idx in sorted(self.blocks.keys()):
            block = self.blocks[idx]
            if block["type"] == "text":
                return block.get("text", "")
        return ""

    def thinking_block(self) -> str | None:
        text = self.final_text()
        m = _THINK_RE.search(text)
        return m.group(1).strip() if m else None

    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.blocks.values() if b["type"] == "tool_use"]


def extract_thinking_from_text(text: str) -> tuple[str | None, str]:
    """Return (thinking_content, text_with_thinking_stripped)."""
    m = _THINK_RE.search(text)
    if not m:
        return None, text
    return m.group(1).strip(), _THINK_RE.sub("", text).strip()


# ── Persistence ───────────────────────────────────────────────────────────────


async def persist_exchange(
    *,
    session_id: str,
    skill: str,
    agent: str,
    request_messages: list[dict[str, Any]],
    system_prompt: str,
    assistant_content: list[dict[str, Any]],
    final_text: str,
    thinking: str | None,
    stop_reason: str | None,
    tool_uses: list[dict[str, Any]],
    prompt_tokens: int,
    completion_tokens: int,
    duration_ms: int,
    error: str | None,
) -> None:
    """One audit_log row + N tool_calls rows for this /v1/messages exchange."""
    request_index = SESSION_REQUEST_INDEX[session_id]
    SESSION_REQUEST_INDEX[session_id] += 1

    full_messages = request_messages + [{"role": "assistant", "content": assistant_content}]

    try:
        await store.write_audit_log(
            session_id=session_id,
            agent_name=agent,
            routine=skill,
            trigger_source=os.environ.get("OBS_TRIGGER_SOURCE", "scheduled"),
            system_prompt=system_prompt or "",
            messages=full_messages,
            tool_rounds=len(tool_uses),
            final_response=final_text,
            finish_reason=stop_reason or ("error" if error else "unknown"),
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            error=error,
            extra_fields={
                "skill_name": skill,
                "request_index": request_index,
                "thinking_block": thinking,
            },
        )
    except TypeError:
        # write_audit_log may not accept extra_fields yet — fall back to direct INSERT
        # so we don't block on a schema-aware-store update.
        log.debug("write_audit_log doesn't accept extra_fields; using direct INSERT")
        await _direct_insert_audit_log(
            session_id=session_id,
            agent=agent,
            skill=skill,
            system_prompt=system_prompt or "",
            messages=full_messages,
            tool_rounds=len(tool_uses),
            final_text=final_text,
            stop_reason=stop_reason,
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            error=error,
            request_index=request_index,
            thinking=thinking,
        )

    for round_idx, tu in enumerate(tool_uses):
        await store.write_tool_call(
            session_id=session_id,
            tool_round=round_idx,
            tool_name=tu.get("name") or "?",
            tool_input=tu.get("input") or {},
            tool_output=None,  # tool result lands in the next request's messages array
            duration_ms=0,
            error=None,
        )


async def _direct_insert_audit_log(
    *,
    session_id: str,
    agent: str,
    skill: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tool_rounds: int,
    final_text: str,
    stop_reason: str | None,
    duration_ms: int,
    prompt_tokens: int,
    completion_tokens: int,
    error: str | None,
    request_index: int,
    thinking: str | None,
) -> None:
    """Direct INSERT bypassing store.write_audit_log so we can populate the new columns."""
    from datetime import datetime, timezone
    from db.schema import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO audit_log
              (session_id, created_at, agent_name, routine, trigger_source,
               system_prompt, messages, tool_rounds, final_response, finish_reason,
               duration_ms, prompt_tokens, completion_tokens, error,
               skill_name, request_index, thinking_block)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
            """,
            session_id,
            datetime.now(timezone.utc).isoformat(),
            agent,
            skill,
            os.environ.get("OBS_TRIGGER_SOURCE", "scheduled"),
            system_prompt,
            json.dumps(messages),
            tool_rounds,
            final_text,
            stop_reason or ("error" if error else "unknown"),
            duration_ms,
            prompt_tokens,
            completion_tokens,
            error,
            skill,
            request_index,
            thinking,
        )


# ── HTTP forwarding paths ─────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
    log.info("proxy ready (vLLM target = %s)", VLLM_ROOT)
    try:
        yield
    finally:
        await app.state.client.aclose()


app = FastAPI(title="obs.proxy", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "vllm": VLLM_ROOT}


@app.get("/live")
async def live_sessions() -> JSONResponse:
    """Snapshot of currently-running sessions for the dashboard."""
    out = []
    for sid, info in LIVE_SESSIONS.items():
        out.append({
            "session_id": sid,
            "skill": info.get("skill"),
            "agent": info.get("agent"),
            "started_at": info.get("started_at"),
            "last_chunk_at": info.get("last_chunk_at"),
            "preview": (info.get("preview") or "")[:240],
            "tokens_so_far": info.get("output_tokens", 0),
        })
    return JSONResponse(out)


@app.get("/stream/{session_id}")
async def stream(session_id: str, request: Request):
    """SSE endpoint the dashboard subscribes to for live tokens."""
    queue = await PUBSUB.subscribe(session_id)

    async def gen() -> AsyncIterator[dict[str, Any]]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}
                    continue
                if chunk is None:
                    break
                # forward as a single SSE event named "raw" carrying the bytes verbatim
                yield {"event": "raw", "data": chunk.decode("utf-8", errors="replace")}
        finally:
            await PUBSUB.unsubscribe(session_id, queue)

    return EventSourceResponse(gen())


@app.post("/skill/{skill}/session/{session_id}/v1/messages")
async def relay_messages(skill: str, session_id: str, request: Request):
    return await _relay(request, skill=skill, session_id=session_id, suffix="/v1/messages")


@app.post("/skill/{skill}/session/{session_id}/v1/messages/count_tokens")
async def relay_count_tokens(skill: str, session_id: str, request: Request):
    return await _relay(request, skill=skill, session_id=session_id, suffix="/v1/messages/count_tokens", passthrough_only=True)


# Fallback: claude CLI invoked outside run_scheduled_skill.sh hits these.
@app.post("/v1/messages")
async def relay_messages_adhoc(request: Request):
    return await _relay(request, skill="adhoc", session_id=str(uuid.uuid4()), suffix="/v1/messages")


@app.post("/v1/messages/count_tokens")
async def relay_count_tokens_adhoc(request: Request):
    return await _relay(request, skill="adhoc", session_id=str(uuid.uuid4()), suffix="/v1/messages/count_tokens", passthrough_only=True)


# ── Request relay (the meat) ─────────────────────────────────────────────────


async def _relay(request: Request, *, skill: str, session_id: str, suffix: str, passthrough_only: bool = False):
    body = await request.body()
    target = f"{VLLM_ROOT}{suffix}"

    if passthrough_only:
        client: httpx.AsyncClient = request.app.state.client
        resp = await client.post(target, content=body, headers={"content-type": "application/json"})
        return JSONResponse(content=resp.json(), status_code=resp.status_code)

    try:
        body_dict = json.loads(body) if body else {}
    except json.JSONDecodeError:
        body_dict = {}
    is_streaming = bool(body_dict.get("stream"))

    agent = derive_agent(skill)
    started_at = time.time()

    LIVE_SESSIONS[session_id] = {
        "session_id": session_id,
        "skill": skill,
        "agent": agent,
        "started_at": started_at,
        "last_chunk_at": started_at,
        "preview": "",
        "output_tokens": 0,
    }

    if is_streaming:
        return StreamingResponse(
            _stream_relay(request, body, target, session_id, skill, agent, body_dict, started_at),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache"},
        )
    return await _nonstream_relay(request, body, target, session_id, skill, agent, body_dict, started_at)


async def _stream_relay(
    request: Request,
    body: bytes,
    target: str,
    session_id: str,
    skill: str,
    agent: str,
    body_dict: dict[str, Any],
    started_at: float,
) -> AsyncIterator[bytes]:
    accumulator = StreamAccumulator()
    error: str | None = None
    client: httpx.AsyncClient = request.app.state.client
    sse_buffer = b""

    try:
        async with client.stream(
            "POST",
            target,
            content=body,
            headers={"content-type": "application/json", "accept": "text/event-stream"},
        ) as resp:
            if resp.status_code != 200:
                error = f"vllm {resp.status_code}: {await _read_text(resp)}"
                payload = json.dumps({"type": "error", "error": {"type": "upstream_error", "message": error}}).encode()
                yield b"event: error\ndata: " + payload + b"\n\n"
                return
            async for raw in resp.aiter_bytes():
                yield raw  # forward to CLI verbatim
                sse_buffer += raw
                LIVE_SESSIONS[session_id]["last_chunk_at"] = time.time()
                # SSE events end with \n\n; consume complete events
                while b"\n\n" in sse_buffer:
                    event_bytes, sse_buffer = sse_buffer.split(b"\n\n", 1)
                    if event_bytes:
                        accumulator.consume(event_bytes)
                        PUBSUB.publish(session_id, event_bytes + b"\n\n")
                # Update preview for /live snapshot (last 240 chars of running text).
                preview = accumulator.final_text()[-240:]
                LIVE_SESSIONS[session_id]["preview"] = preview
                LIVE_SESSIONS[session_id]["output_tokens"] = accumulator.usage.get("output_tokens", 0)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.exception("stream_relay failed for session %s", session_id)
    finally:
        await _persist_and_cleanup(session_id, skill, agent, body_dict, accumulator, started_at, error)


async def _nonstream_relay(
    request: Request,
    body: bytes,
    target: str,
    session_id: str,
    skill: str,
    agent: str,
    body_dict: dict[str, Any],
    started_at: float,
) -> JSONResponse:
    accumulator = StreamAccumulator()
    error: str | None = None
    client: httpx.AsyncClient = request.app.state.client
    try:
        resp = await client.post(target, content=body, headers={"content-type": "application/json"})
        resp_data: dict[str, Any] = resp.json() if resp.content else {}
        if resp.status_code != 200:
            error = f"vllm {resp.status_code}: {json.dumps(resp_data)[:400]}"
            await _persist_and_cleanup(session_id, skill, agent, body_dict, accumulator, started_at, error)
            return JSONResponse(content=resp_data, status_code=resp.status_code)
        # Reconstruct accumulator state from the non-streaming response shape.
        accumulator.message_id = resp_data.get("id")
        accumulator.model = resp_data.get("model")
        accumulator.stop_reason = resp_data.get("stop_reason")
        usage = resp_data.get("usage") or {}
        accumulator.usage["input_tokens"] = usage.get("input_tokens", 0)
        accumulator.usage["output_tokens"] = usage.get("output_tokens", 0)
        for idx, block in enumerate(resp_data.get("content", []) or []):
            btype = block.get("type")
            if btype == "text":
                accumulator.blocks[idx] = {"type": "text", "text": block.get("text", "")}
            elif btype == "tool_use":
                accumulator.blocks[idx] = {
                    "type": "tool_use",
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": block.get("input") or {},
                }
        await _persist_and_cleanup(session_id, skill, agent, body_dict, accumulator, started_at, error)
        return JSONResponse(content=resp_data, status_code=resp.status_code)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.exception("nonstream_relay failed for session %s", session_id)
        await _persist_and_cleanup(session_id, skill, agent, body_dict, accumulator, started_at, error)
        return JSONResponse(content={"type": "error", "error": {"message": error}}, status_code=502)


async def _read_text(resp: httpx.Response) -> str:
    try:
        return (await resp.aread()).decode("utf-8", errors="replace")
    except Exception:
        return ""


async def _persist_and_cleanup(
    session_id: str,
    skill: str,
    agent: str,
    body_dict: dict[str, Any],
    accumulator: StreamAccumulator,
    started_at: float,
    error: str | None,
) -> None:
    try:
        # Pull system prompt out of the request — Anthropic shape allows either string or list of blocks.
        sys_field = body_dict.get("system")
        if isinstance(sys_field, list):
            system_prompt = "\n".join(b.get("text", "") for b in sys_field if isinstance(b, dict))
        else:
            system_prompt = sys_field or ""

        thinking, _ = extract_thinking_from_text(accumulator.final_text())

        await persist_exchange(
            session_id=session_id,
            skill=skill,
            agent=agent,
            request_messages=body_dict.get("messages") or [],
            system_prompt=system_prompt,
            assistant_content=accumulator.assistant_content(),
            final_text=accumulator.final_text(),
            thinking=thinking,
            stop_reason=accumulator.stop_reason,
            tool_uses=accumulator.tool_uses(),
            prompt_tokens=accumulator.usage.get("input_tokens", 0),
            completion_tokens=accumulator.usage.get("output_tokens", 0),
            duration_ms=int((time.time() - started_at) * 1000),
            error=error,
        )
    except Exception:
        log.exception("persist_exchange failed for session %s", session_id)
    finally:
        PUBSUB.close(session_id)
        LIVE_SESSIONS.pop(session_id, None)
