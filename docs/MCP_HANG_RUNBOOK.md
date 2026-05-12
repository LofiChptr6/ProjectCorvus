# MCP hang runbook

**Symptom:** A Claude Code session tries to call an `mcp__ibkr-trading__*`
tool and either times out, hangs for tens of seconds to many minutes, or
returns `Connection closed`. Other Claude windows on the same machine may
be fine.

**Important:** the MCP server itself (`mcp_server.py`) is **not** the
slow part. The testbench in `scripts/mcp_testbench.py` proves the
end-to-end stdio path runs in ~1.2 seconds wall-time (cold spawn → tools
list → 8 tool calls → done) with every individual call returning in
1–150ms. Any multi-second hang is almost certainly on the **Claude CLI
side of the stdio pipe**, not in `mcp_server.py`.

Until we capture a live hang and trace the CLI process, treat the
symptom as a CLI-pipe problem, not a server problem.

---

## When it happens: capture, don't restart

The temptation is to restart things and move on. Don't — every restart
destroys evidence. Capture first.

### Step 1 — Take the snapshot

In a **separate terminal** (not the hung Claude window), run:

```bash
# Identify which mcp_server.py is hung (the one whose parent is the hung
# Claude session). All four columns matter:
ps -eo pid,ppid,etime,stat,cmd | grep -E "mcp_server.py|ccd-cli/" | grep -v grep
```

Look for:
- A `mcp_server.py` whose `etime` (process age) is short but whose `stat`
  is `Ssl` (sleeping on syscall). Healthy.
- A `mcp_server.py` whose parent (a `ccd-cli/.../node` process) is in
  state `D` (uninterruptible I/O wait) or `Z` (zombie). The CLI side is
  the suspect.

### Step 2 — Trace the suspect

```bash
PID=<the mcp_server.py PID from above>
# Whose syscall is it parked in?
sudo strace -p $PID -e trace=read,write,poll,select 2>&1 | head -50
# (Ctrl-C after a few seconds; we just want to see what fd it's waiting on)
```

Common patterns:

- **`read(0, ...)` looping with nothing arriving:** the CLI is alive but
  not sending any JSON-RPC. Means the CLI is stuck before the dispatch.
- **`write(1, ...)` blocking:** the MCP server has a response ready but
  the CLI's stdout pipe is full / unread. Means the CLI stopped reading.
- **No syscalls at all (process truly idle):** server finished a call
  and is waiting for the next one. Server is fine, CLI hasn't called.

In all three, the MCP server side is healthy. The CLI is the problem.

### Step 3 — Confirm by re-running the testbench

```bash
cd "/home/tianyizhang/opus trading"
.venv/bin/python scripts/mcp_testbench.py --repeat=2 --hang-timeout=60
```

Expect <2s total. If the testbench succeeds while a Claude window is
still hung on the same machine, that's proof the server is healthy and
the hung pipe is CLI-internal.

### Step 4 — Capture the CLI-side state

Find the hung `ccd-cli` (Claude Code) parent PID — it's the `ppid` of
the suspect `mcp_server.py`. Then:

```bash
CLI_PID=<the ccd-cli PID>
# Stack trace of every thread inside the Node process
gdb -batch -ex "thread apply all bt" -p $CLI_PID 2>&1 | tee /tmp/cli-hang-$CLI_PID.txt
# Open file descriptors — does it still hold the stdio pipes to mcp_server.py?
ls -l /proc/$CLI_PID/fd 2>&1 | grep pipe
```

`gdb` may need `sudo` and `debuginfo-install nodejs` to give names. If
`gdb` is unavailable, `/proc/$CLI_PID/stack` also shows the kernel-side
wait point.

Save `/tmp/cli-hang-*.txt`, the strace transcript, and the output of
`ps -eo pid,ppid,etime,stat,cmd` to share. That's what's needed to
diagnose where the CLI is parking.

### Step 5 — Recover

Once captured:

```bash
# Kill just the hung CLI process — leaves other Claude windows alone.
# The OS reaps the orphaned mcp_server.py automatically.
kill -TERM $CLI_PID
# Wait 5s; if still alive, escalate:
kill -KILL $CLI_PID
```

Then start a fresh Claude window. The new window spawns a new
`mcp_server.py` (~1s cold start) and tool calls return to normal.

---

## What we ruled out (so don't waste time re-checking)

- **`mcp_server.py` startup latency:** ~0.6s init + ~30ms per tool. Not
  the issue.
- **`init_db()` / `_ensure_init_light()`:** runs once per subprocess
  lifetime and adds < 100ms after the first call.
- **IBKR daemon at :7790:** has its own healthz; tool calls log clean
  200s.
- **Postgres pool exhaustion:** `command_timeout=30` would bound any
  single query. Hangs would be tens of seconds, not minutes.
- **The vLLM proxy at :8001:** unrelated to MCP — that path is for the
  Python pipeline (sector reviews), not for the Claude CLI.

---

## Quick-check script

If hangs start happening repeatedly, run this every time to confirm the
server is healthy before going further:

```bash
cd "/home/tianyizhang/opus trading"
timeout 60 .venv/bin/python scripts/mcp_testbench.py --repeat=1 --hang-timeout=30 2>&1 | head -25
```

Expect every row to show `ok=Y` and total wall under 2 seconds. If
that's true, the next hang is CLI-side — proceed to step 1.

---

## Open question

We haven't yet captured a live hang to know what specifically the CLI
parks on (its own internal state, the stdio pipe, or something else).
Next time you hit one, follow Steps 1–4 and share the captures. We'll
update this runbook with the actual root cause.
