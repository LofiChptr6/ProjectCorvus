#!/usr/bin/env bash
# Mode B — detached background (Linux port of start_concierge_bg.bat).
#
# Forks concierge.service into the background via setsid + nohup so it
# survives terminal closure. PID is written to data/concierge.lock by the
# service itself; we double-check the lock first to fail fast on double-start.
#
# Tail logs:  tail -f logs/concierge.log
# Stop:       scripts/stop_concierge.sh
#
# This is "lighter" than the systemd-user unit (Mode C) — it does NOT
# auto-start at login. Run this whenever you want the concierge up.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOCK="data/concierge.lock"
if [[ -f "$LOCK" ]]; then
    PID="$(cat "$LOCK" 2>/dev/null || true)"
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
        echo "Concierge already running as PID $PID. Lockfile: $LOCK" >&2
        exit 1
    fi
    echo "Stale lock at $LOCK (PID $PID not alive) — removing." >&2
    rm -f "$LOCK"
fi

mkdir -p logs data
PY="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
    echo "ERROR: ${PY} not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

# setsid → new session, detaches from this TTY. nohup ignores SIGHUP on
# parent exit. Stdout/stderr go to logs/concierge.log (the service also has
# its own RotatingFileHandler — this catches anything before logging is up).
setsid nohup "$PY" -m concierge.service >>logs/concierge.log 2>&1 < /dev/null &
disown || true

# Give the service a moment to write its lockfile before we report the PID.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.2
    if [[ -f "$LOCK" ]]; then
        echo "Concierge launched in the background. PID $(cat "$LOCK")."
        echo "Tail logs:  tail -f logs/concierge.log"
        echo "Stop:       scripts/stop_concierge.sh"
        exit 0
    fi
done

echo "WARNING: lockfile not written within 2s. Check logs/concierge.log for startup errors." >&2
exit 1
