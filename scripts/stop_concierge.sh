#!/usr/bin/env bash
# Stop a running concierge launched via start_concierge.sh.
# Reads PID from data/concierge.lock, sends SIGTERM, waits up to 10s for
# graceful shutdown, then SIGKILLs if still alive. Removes the lock either way.
#
# For systemd-managed installs, prefer:  systemctl --user stop trading-concierge

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOCK="$REPO_ROOT/data/concierge.lock"

if [[ ! -f "$LOCK" ]]; then
    echo "No lock file at $LOCK — concierge not running."
    exit 0
fi

PID="$(cat "$LOCK" | tr -d '[:space:]')"
if [[ -z "$PID" ]] || ! [[ "$PID" =~ ^[0-9]+$ ]]; then
    echo "Lock file is corrupt (PID=$PID); removing."
    rm -f "$LOCK"
    exit 0
fi

if ! kill -0 "$PID" 2>/dev/null; then
    echo "Stale lock (PID $PID not running); cleaning up."
    rm -f "$LOCK"
    exit 0
fi

echo "Sending SIGTERM to concierge (PID $PID)..."
kill -TERM "$PID"

# Wait up to 10s for graceful shutdown.
for _ in {1..20}; do
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "Concierge stopped."
        rm -f "$LOCK"
        exit 0
    fi
    sleep 0.5
done

echo "Concierge did not exit in 10s — sending SIGKILL."
kill -KILL "$PID" 2>/dev/null || true
rm -f "$LOCK"
