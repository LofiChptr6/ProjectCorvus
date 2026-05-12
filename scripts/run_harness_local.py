"""Local-LLM harness runner for a Claude-Code-style skill.

Replaces `claude -p "/<skill-name>"` for skills that need full MCP tool
access but should run against the local vLLM, not the Anthropic API.

Lifecycle:
  1. Read `.claude/commands/<skill>.md` (strip frontmatter) → system prompt
  2. Spawn `mcp_server.py` as a child via stdio (same launch path Claude
     CLI uses, per `.mcp.json`)
  3. Handshake: `initialize`, `notifications/initialized`, `tools/list`
  4. Translate Anthropic-shape tool schemas to OpenAI function-tool shape
  5. Build an `AsyncOpenAI` client via `pipelines.llm_client.make_client`
     (routes through obs/proxy.py for audit logging)
  6. Tool-call loop with `pipelines.tool_loop.run`, dispatching each call
     back through the MCP-stdio subprocess via JSON-RPC `tools/call`
  7. Shut down the subprocess cleanly; persist the final assistant text
     to `logs/<skill>.harness-local.log`

Usage:
    python scripts/run_harness_local.py mike-morning
    python scripts/run_harness_local.py mike-midday --max-iter 30
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import find_dotenv, load_dotenv
    found = find_dotenv(usecwd=True) or str(REPO_ROOT / ".env")
    if Path(found).exists():
        load_dotenv(found)
except Exception:
    pass

PYTHON_BIN = REPO_ROOT / ".venv" / "bin" / "python"
SERVER_SCRIPT = REPO_ROOT / "mcp_server.py"
SKILL_DIR = REPO_ROOT / ".claude" / "commands"
LOG_DIR = REPO_ROOT / "logs"


# ── MCP stdio plumbing ────────────────────────────────────────────────────────


async def _read_line(stream: asyncio.StreamReader) -> dict | None:
    line = await stream.readline()
    if not line:
        return None
    text = line.decode("utf-8").strip()
    if not text:
        return None
    if text.startswith("{"):
        return json.loads(text)
    if text.lower().startswith("content-length:"):
        length = int(text.split(":", 1)[1].strip())
        await stream.readline()  # blank line
        body = await stream.readexactly(length)
        return json.loads(body.decode("utf-8"))
    return None


async def _send(writer: asyncio.StreamWriter, msg: dict) -> None:
    writer.write((json.dumps(msg) + "\n").encode("utf-8"))
    await writer.drain()


async def _await_id(
    stream: asyncio.StreamReader, want_id: int, timeout_s: float,
) -> dict:
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no MCP response for id={want_id} in {timeout_s:.0f}s")
        msg = await asyncio.wait_for(_read_line(stream), timeout=remaining)
        if msg is None:
            raise RuntimeError("MCP subprocess closed stdout unexpectedly")
        if msg.get("id") == want_id:
            return msg


# ── Skill file ────────────────────────────────────────────────────────────────


def _load_skill_prompt(skill_name: str) -> str:
    path = SKILL_DIR / f"{skill_name}.md"
    if not path.exists():
        raise FileNotFoundError(f"skill not found: {path}")
    text = path.read_text(encoding="utf-8")
    # Strip a leading YAML frontmatter block if present.
    if text.startswith("---"):
        m = re.match(r"^---\s*\n(.*?\n)?---\s*\n(.*)$", text, re.DOTALL)
        if m:
            text = m.group(2)
    return text.strip()


# ── OpenAI tool schema translation ────────────────────────────────────────────


def _mcp_tools_to_openai(mcp_tools: list[dict]) -> list[dict]:
    """MCP `tools/list` returns Anthropic-ish `{name, description, inputSchema}`.
    Translate to OpenAI `{type:"function", function:{...}}`."""
    out = []
    for t in mcp_tools:
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
            },
        })
    return out


# ── Main orchestration ───────────────────────────────────────────────────────


async def run_harness(skill_name: str, max_iter: int, max_tokens: int) -> int:
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{skill_name}.harness-local.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, mode="a", encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )
    log = logging.getLogger("harness")
    session_id = str(uuid.uuid4())
    log.info("starting skill=%s session=%s max_iter=%d", skill_name, session_id, max_iter)

    system_prompt = _load_skill_prompt(skill_name)
    user_prompt = (
        "Execute the routine described in your system prompt now. "
        "Use the tools available to gather data, perform the analysis, "
        "and send the required Telegram messages. When the routine is "
        "complete, reply with a one-paragraph summary of what you did."
    )

    # Spawn MCP server subprocess
    proc = await asyncio.create_subprocess_exec(
        str(PYTHON_BIN), str(SERVER_SCRIPT),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(REPO_ROOT),
        env={**os.environ},
        limit=2 ** 24,
    )
    assert proc.stdin and proc.stdout

    try:
        # MCP handshake
        await _send(proc.stdin, {
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "run_harness_local", "version": "0.1"},
            },
        })
        await _await_id(proc.stdout, 0, timeout_s=60)
        await _send(proc.stdin, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        # Discover tools
        await _send(proc.stdin, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools_resp = await _await_id(proc.stdout, 1, timeout_s=60)
        mcp_tools = (tools_resp.get("result") or {}).get("tools", [])
        openai_tools = _mcp_tools_to_openai(mcp_tools)
        log.info("discovered %d MCP tools", len(mcp_tools))

        # LLM client (routes through obs/proxy for audit)
        from pipelines.llm_client import make_client
        llm = make_client(skill_name=skill_name, session_id=session_id)
        log.info("llm base_url=%s model=%s", llm.base_url, llm.model)

        # Dispatch closure — owns the next-rpc-id counter
        rpc_counter = {"n": 2}

        async def dispatch(tool_name: str, args: dict) -> str:
            rpc_counter["n"] += 1
            rid = rpc_counter["n"]
            await _send(proc.stdin, {
                "jsonrpc": "2.0", "id": rid, "method": "tools/call",
                "params": {"name": tool_name, "arguments": args},
            })
            try:
                resp = await _await_id(proc.stdout, rid, timeout_s=120)
            except Exception as exc:
                return json.dumps({"error": f"mcp dispatch failed: {type(exc).__name__}: {exc}"})
            if "error" in resp:
                return json.dumps({"error": resp["error"]})
            # MCP returns content as a list of {type:'text', text:'...'} parts
            parts = (resp.get("result") or {}).get("content", [])
            text_parts = [p.get("text", "") for p in parts if p.get("type") == "text"]
            return "\n".join(text_parts) if text_parts else json.dumps(resp.get("result"))

        # Manual tool-call loop (the pipelines.tool_loop.run version assumes
        # Anthropic-shape schemas; we already have OpenAI-shape from the
        # translation above, so drive the loop directly here)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        finish_reason = "unknown"
        for iteration in range(max_iter + 1):
            try:
                resp = await llm.client.chat.completions.create(
                    model=llm.model, max_tokens=max_tokens,
                    temperature=0.4,
                    tools=openai_tools if openai_tools else None,
                    tool_choice="auto" if openai_tools else None,
                    messages=messages,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
            except Exception as exc:
                log.error("LLM call failed iter=%d: %s", iteration, exc)
                finish_reason = "llm_error"
                break

            choice = resp.choices[0]
            msg = choice.message
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in (msg.tool_calls or [])
                ] or None,
            })

            if not msg.tool_calls:
                finish_reason = "stop"
                if msg.content:
                    log.info("final assistant text (%d chars): %s",
                             len(msg.content), msg.content[:600])
                break

            # Dispatch each tool call
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                log.info("iter=%d tool=%s args=%s",
                         iteration, tc.function.name,
                         json.dumps(args)[:200])
                result = await dispatch(tc.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result[:8000],  # cap to keep context bounded
                })
        else:
            finish_reason = "max_iter"
            log.warning("hit max_iter=%d without natural stop", max_iter)

        log.info("done skill=%s finish=%s iters=%d",
                 skill_name, finish_reason, iteration)
        return 0 if finish_reason in ("stop", "max_iter") else 1
    finally:
        try:
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("skill", help="Skill name (matches .claude/commands/<skill>.md)")
    p.add_argument("--max-iter", type=int, default=25,
                   help="Hard cap on tool-loop iterations (default 25)")
    p.add_argument("--max-tokens", type=int, default=2048,
                   help="Per-completion max output tokens (default 2048)")
    args = p.parse_args()
    return asyncio.run(run_harness(args.skill, args.max_iter, args.max_tokens))


if __name__ == "__main__":
    raise SystemExit(main())
