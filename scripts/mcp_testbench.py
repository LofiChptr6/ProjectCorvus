"""MCP testbench — measures end-to-end latency of stdio tool calls.

Spawns a fresh `mcp_server.py` subprocess via the same stdio path Claude
uses (per `.mcp.json`), then drives a configurable sequence of JSON-RPC
requests. Each request is timed individually, and the script can run a
matrix (cold-start vs. warm, fast tools vs. trading-tool init paths).

Usage:
    .venv/bin/python scripts/mcp_testbench.py            # default suite
    .venv/bin/python scripts/mcp_testbench.py --tool=get_market_status
    .venv/bin/python scripts/mcp_testbench.py --repeat=5 --hang-timeout=60

Outputs a per-request line `tool_name  status  duration_ms  notes` plus
a summary table. Exit code is non-zero on timeout or RPC error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_BIN = REPO_ROOT / ".venv" / "bin" / "python"
SERVER_SCRIPT = REPO_ROOT / "mcp_server.py"


JSONRPC_INIT = {
    "jsonrpc": "2.0", "id": 0, "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "mcp-testbench", "version": "0.1"},
    },
}
JSONRPC_INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
JSONRPC_LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def _call(rpc_id: int, tool_name: str, args: dict) -> dict:
    return {
        "jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
        "params": {"name": tool_name, "arguments": args},
    }


# Default suite — picks tools that exercise different init paths.
DEFAULT_SUITE = [
    # Light path (DB only): _ensure_init_light
    ("get_market_status", {}),
    ("get_kill_switch_status", {}),
    ("list_pending_proposals", {}),
    # Trading path: _ensure_init (DB + IBKR daemon healthz)
    ("get_balances", {}),
    ("get_positions", {}),
    ("get_open_orders", {}),
    # Read-heavy DB path
    ("get_agent_list", {}),
    ("get_pnl_summary", {"period": "today"}),
]


async def _read_message(stream: asyncio.StreamReader) -> dict | None:
    """Read one JSON-RPC message framed as Content-Length headers (LSP-style)
    OR newline-delimited (the python `mcp` server uses ndjson by default)."""
    line = await stream.readline()
    if not line:
        return None
    text = line.decode("utf-8").strip()
    if not text:
        return None
    if text.startswith("{"):  # ndjson
        return json.loads(text)
    # else: LSP-style framing
    if text.lower().startswith("content-length:"):
        length = int(text.split(":", 1)[1].strip())
        # consume blank line
        await stream.readline()
        body = await stream.readexactly(length)
        return json.loads(body.decode("utf-8"))
    return None


async def _send(stream: asyncio.StreamWriter, msg: dict) -> None:
    payload = (json.dumps(msg) + "\n").encode("utf-8")
    stream.write(payload)
    await stream.drain()


async def _drain_until(
    proc_stdout: asyncio.StreamReader,
    want_id: int,
    timeout: float,
) -> tuple[dict | None, float, str]:
    """Read messages until we see a response for `want_id`. Returns
    (response, duration_s, note)."""
    start = time.monotonic()
    deadline = start + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, time.monotonic() - start, f"timeout after {timeout:.1f}s"
        try:
            msg = await asyncio.wait_for(_read_message(proc_stdout), timeout=remaining)
        except asyncio.TimeoutError:
            return None, time.monotonic() - start, f"timeout after {timeout:.1f}s"
        if msg is None:
            return None, time.monotonic() - start, "stdout EOF (subprocess died)"
        if msg.get("id") == want_id:
            return msg, time.monotonic() - start, "ok"
        # else it's a log / notification — keep reading


async def run_suite(
    suite: list[tuple[str, dict]],
    repeat: int,
    hang_timeout: float,
) -> list[dict]:
    results: list[dict] = []
    proc = await asyncio.create_subprocess_exec(
        str(PYTHON_BIN), str(SERVER_SCRIPT),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(REPO_ROOT),
        env={**os.environ},
        limit=2 ** 24,  # 16 MiB; tools/list response can exceed the asyncio 64K default
    )
    assert proc.stdin and proc.stdout
    spawn_t = time.monotonic()
    try:
        # Handshake
        await _send(proc.stdin, JSONRPC_INIT)
        init_resp, init_dur, init_note = await _drain_until(proc.stdout, 0, hang_timeout)
        results.append({
            "step": "initialize", "rpc_id": 0,
            "duration_s": init_dur, "note": init_note,
            "ok": init_resp is not None and "result" in (init_resp or {}),
        })
        await _send(proc.stdin, JSONRPC_INITIALIZED)
        await _send(proc.stdin, JSONRPC_LIST_TOOLS)
        list_resp, list_dur, list_note = await _drain_until(proc.stdout, 1, hang_timeout)
        results.append({
            "step": "tools/list", "rpc_id": 1,
            "duration_s": list_dur, "note": list_note,
            "ok": list_resp is not None and "result" in (list_resp or {}),
        })

        rpc_id = 2
        for cycle in range(repeat):
            for tool_name, args in suite:
                req = _call(rpc_id, tool_name, args)
                await _send(proc.stdin, req)
                resp, dur, note = await _drain_until(proc.stdout, rpc_id, hang_timeout)
                ok = resp is not None and "result" in (resp or {})
                err = None
                if resp and "error" in resp:
                    err = resp["error"]
                results.append({
                    "step": f"call:{tool_name}",
                    "cycle": cycle, "rpc_id": rpc_id,
                    "duration_s": dur, "note": note,
                    "ok": ok, "error": err,
                })
                rpc_id += 1
        # Wall-clock since spawn
        results.append({
            "step": "total_wall", "rpc_id": None,
            "duration_s": time.monotonic() - spawn_t, "note": "spawn→done",
            "ok": True,
        })
    finally:
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        # Collect any stderr for diagnostics
        if proc.stderr:
            err_bytes = await proc.stderr.read()
            if err_bytes:
                results.append({
                    "step": "stderr", "rpc_id": None,
                    "duration_s": 0, "note": err_bytes.decode("utf-8", errors="replace")[:2000],
                    "ok": True,
                })
    return results


def _summary(results: list[dict]) -> str:
    lines = []
    lines.append(f"{'step':<32}  {'ok':<3}  {'dur(s)':>8}  note")
    lines.append("-" * 78)
    for r in results:
        if r["step"] == "stderr":
            continue
        lines.append(
            f"{r['step']:<32}  {('Y' if r['ok'] else 'N'):<3}  "
            f"{r['duration_s']:>8.3f}  {r.get('note','') or ''}"
        )
    stderr_rows = [r for r in results if r["step"] == "stderr"]
    if stderr_rows:
        lines.append("")
        lines.append("=== subprocess stderr ===")
        lines.append(stderr_rows[0]["note"])
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=2,
                    help="how many times to walk the suite (warm-call observation)")
    ap.add_argument("--hang-timeout", type=float, default=60.0,
                    help="per-RPC wait ceiling in seconds")
    ap.add_argument("--tool", default=None,
                    help="single-tool mode; overrides default suite")
    args = ap.parse_args()

    if args.tool:
        suite = [(args.tool, {})]
    else:
        suite = DEFAULT_SUITE

    results = asyncio.run(run_suite(suite, args.repeat, args.hang_timeout))
    print(_summary(results))
    any_failed = any(not r["ok"] for r in results if r["step"] != "stderr")
    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
