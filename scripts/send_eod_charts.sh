#!/usr/bin/env bash
# End-of-day Telegram broadcast: 10 sector agents + desk, 7-day P&L curves.
# Triggered by cron Mon-Fri after market close.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

mkdir -p logs
LOG="logs/send_eod_charts.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] send_eod_charts start" >> "$LOG"
"$REPO_ROOT/.venv/bin/python" -m scripts.send_eod_charts >> "$LOG" 2>&1
ec=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] send_eod_charts end (exit=$ec)" >> "$LOG"
exit $ec
