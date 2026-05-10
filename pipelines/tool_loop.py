"""OpenAI tool-loop runner.

Cloned from concierge/chat.py:60-75 + 133-187, parameterized for sector skills.
The loop drives an OpenAI-compatible chat completion (vLLM-served) until the
model emits a final assistant message with no tool_calls, OR `max_iter` is hit.

Returns the final assistant text plus the full message history for audit.
"""
from __future__ import annotations

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
        try:
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
            resp = await client.chat.completions.create(**kwargs)
        except Exception as exc:
            log.exception("LLM call failed in tool_loop")
            final_text = f"⚠️ pipeline error: {type(exc).__name__}: {exc}"
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
