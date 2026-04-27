"""Routes tool calls from Claude to the correct execute() function."""

from __future__ import annotations

import logging
import time
from typing import Optional

import db.store as store
from tools.registry import get_executor

log = logging.getLogger(__name__)


async def execute(
    tool_name: str,
    tool_input: dict,
    session_id: Optional[str] = None,
    tool_round: int = 0,
) -> str:
    executor = get_executor(tool_name)
    start = time.monotonic()
    error: Optional[str] = None
    output: Optional[str] = None

    try:
        output = await executor(**tool_input)
    except Exception as exc:
        error = str(exc)
        output = f'{{"error": "{error}"}}'
        log.error("Tool %s failed: %s", tool_name, exc)

    duration_ms = int((time.monotonic() - start) * 1000)

    if session_id:
        await store.write_tool_call(
            session_id=session_id,
            tool_round=tool_round,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=output,
            duration_ms=duration_ms,
            error=error,
        )

    return output or ""
