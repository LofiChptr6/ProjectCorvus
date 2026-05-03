#!/usr/bin/env bash
# Hourly per-agent state snapshot — deterministic, no LLM, no IBKR.
# Triggered by cron every hour (5 * * * *) regardless of trading hours.
# Logs to logs/refresh_agent_state.log; non-zero exit if the script blows up.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

mkdir -p logs
LOG="logs/refresh_agent_state.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] refresh_agent_state start" >> "$LOG"
"$REPO_ROOT/.venv/bin/python" -m scripts.refresh_agent_state >> "$LOG" 2>&1
ec=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] refresh_agent_state end (exit=$ec)" >> "$LOG"
exit $ec
