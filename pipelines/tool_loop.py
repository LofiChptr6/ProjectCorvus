"""OpenAI tool-loop runner.

Cloned from concierge/chat.py:60-75 + 133-187, parameterized for sector skills.
The loop drives an OpenAI-compatible chat completion (vLLM-served) until the
model emits a final assistant message with no tool_calls, OR `max_iter` is hit.

Returns the final assistant text plus the full message history for audit.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

log = logging.getLogger(__name__)


@dataclass
class ToolLoopResult:
    final_text: str
    history: list[dict[str, Any]]
    iterations: int
    finish_reason: str
    tool_call_log: list[dict[str, Any]] = field(default_factory=list)


def to_openai_tools(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Anthropic-shape `{name,description,input_schema}` to
    OpenAI function-tool shape `{type:"function", function:{...}}`."""
    out = []
    for s in schemas:
        out.append({
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s.get("description", ""),
                "parameters": s.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return out


async def run(
    *,
    client: AsyncOpenAI,
    model: str,
    system: str,
    user: str,
    tool_schemas: list[dict[str, Any]],
    dispatch_fn: Callable[[str, dict[str, Any]], Awaitable[str]],
    max_iter: int = 6,
    max_tokens: int = 2048,
    temperature: float = 0.7,
    disable_thinking: bool = False,
) -> ToolLoopResult:
    """Run a vLLM tool-use loop. Returns final text + history.

    Behaviour:
    - Initial messages: [system, user].
    - Each iteration: chat.completions.create → if tool_calls, dispatch each,
      append role=tool messages with results, repeat. Otherwise stop with the
      assistant's final text.
    - max_iter is the limit on iterations, not tool calls — one iteration may
      dispatch multiple tools in parallel.
    """
    tools = to_openai_tools(tool_schemas) if tool_schemas else None

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    tool_call_log: list[dict[str, Any]] = []
    final_text = ""
    finish_reason = "unknown"

    extra_body = {"chat_template_kwargs": {"enable_thinking": False}} if disable_thinking else None

    for iteration in range(max_iter + 1):
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "tools": tools,
            "tool_choice": "auto" if tools else None,
            "messages": messages,
        }
        if extra_body:
            kwargs["extra_body"] = extra_body

        # Transient-failure retry: one extra attempt with 1s backoff on
        # timeout / network errors. vLLM occasionally stalls under contention
        # and a single retry is usually enough; >1 retry indicates the LLM
        # itself is down and we should surface the error.
        resp = None
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                resp = await client.chat.completions.create(**kwargs)
                break
            except (asyncio.TimeoutError, ConnectionError) as exc:
                last_exc = exc
                if attempt == 0:
                    log.warning(
                        "LLM call transient failure (iter=%d attempt=%d): %s: %s — "
                        "retrying in 1s",
                        iteration, attempt, type(exc).__name__, exc,
                    )
                    await asyncio.sleep(1.0)
                    continue
                log.exception("LLM call failed after retry")
            except Exception as exc:
                last_exc = exc
                # httpx.TimeoutException is one of httpx.NetworkError's
                # siblings; openai may also wrap timeouts in its own classes.
                # String-match the type name so we catch them without adding
                # the httpx/openai dep imports here.
                tname = type(exc).__name__
                if "Timeout" in tname or "Connect" in tname or "Network" in tname:
                    if attempt == 0:
                        log.warning(
                            "LLM call transient failure (iter=%d attempt=%d): %s: %s — "
                            "retrying in 1s",
                            iteration, attempt, tname, exc,
                        )
                        await asyncio.sleep(1.0)
                        continue
                # Non-transient: don't retry.
                log.exception("LLM call failed (non-transient)")
                break

        if resp is None:
            final_text = (
                f"⚠️ pipeline error: {type(last_exc).__name__}: {last_exc}"
                if last_exc is not None else
                "⚠️ pipeline error: unknown LLM failure"
            )
            finish_reason = "error"
            break

        choice = resp.choices[0]
        msg = choice.message
        finish_reason = choice.finish_reason or "stop"

        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": msg.content or "",
        }
        if msg.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant_msg)

        # No tool calls? We're done.
        if finish_reason != "tool_calls" or not msg.tool_calls:
            final_text = msg.content or ""
            break

        # Dispatch every tool call from this iteration.
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await dispatch_fn(name, args)
            tool_call_log.append({
                "iteration": iteration,
                "name": name,
                "args": args,
                "result": result[:500] if isinstance(result, str) else str(result)[:500],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

        if iteration == max_iter:
            final_text = (
                "(tool-iteration cap hit; the agent did not converge in time)"
            )
            finish_reason = "max_iter"
            messages.append({"role": "assistant", "content": final_text})
            break

    return ToolLoopResult(
        final_text=final_text,
        history=messages,
        iterations=iteration,
        finish_reason=finish_reason,
        tool_call_log=tool_call_log,
    )
