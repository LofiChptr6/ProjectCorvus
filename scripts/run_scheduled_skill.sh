#!/usr/bin/env bash
# Generic launcher for Claude Code scheduled skills on Linux/macOS.
# Mirrors run_scheduled_skill.bat — cron should call:
#   /path/to/trading/scripts/run_scheduled_skill.sh <skill-name> [--dev|--force]

set -u

SKILL="${1:-}"
FLAG="${2:-}"

if [[ -z "$SKILL" ]]; then
    echo "ERROR: No skill name provided." >&2
    echo "Usage: run_scheduled_skill.sh <skill-name> [--dev|--force]" >&2
    exit 1
fi

DEV_PREFIX=""
case "${FLAG,,}" in
    --dev|--force)
        DEV_PREFIX="DEV-MODE: For this run only, SKIP all STEP 0 skip-fast guards: market_closed, quiet_window, kill_switch, and was_open checks. Run the full review using whatever stale data is available so the user can see your thinking. Prefix every Telegram message with [DEV] so it is not confused with live signal. Do NOT place any real orders -- analysis only."
        ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Per-skill IBKR clientId (must match run_scheduled_skill.bat)
case "$SKILL" in
    mike-morning)    export IBKR_CLIENT_ID=11 ;;
    mike-midday)     export IBKR_CLIENT_ID=12 ;;
    cassidy-evening) export IBKR_CLIENT_ID=13 ;;
    hourly-review)   export IBKR_CLIENT_ID=14 ;;
    rex-review)      export IBKR_CLIENT_ID=15 ;;
    maya-review)     export IBKR_CLIENT_ID=16 ;;
    atlas-review)    export IBKR_CLIENT_ID=17 ;;
    titan-review)    export IBKR_CLIENT_ID=18 ;;
    vera-review)     export IBKR_CLIENT_ID=19 ;;
    fab-review)      export IBKR_CLIENT_ID=20 ;;
    trump-review)    export IBKR_CLIENT_ID=21 ;;
    rex-evening)     export IBKR_CLIENT_ID=22 ;;
    maya-evening)    export IBKR_CLIENT_ID=23 ;;
    atlas-evening)   export IBKR_CLIENT_ID=24 ;;
    titan-evening)   export IBKR_CLIENT_ID=25 ;;
    fab-evening)     export IBKR_CLIENT_ID=26 ;;
    trump-evening)   export IBKR_CLIENT_ID=27 ;;
    vera-evening)    export IBKR_CLIENT_ID=28 ;;
    iron-review)     export IBKR_CLIENT_ID=29 ;;
    volt-review)     export IBKR_CLIENT_ID=30 ;;
    iron-evening)    export IBKR_CLIENT_ID=31 ;;
    volt-evening)    export IBKR_CLIENT_ID=32 ;;
    mike-allocator)  export IBKR_CLIENT_ID=33 ;;
    fabless-review)  export IBKR_CLIENT_ID=34 ;;
    fabless-evening) export IBKR_CLIENT_ID=35 ;;
    sector-archivist) export IBKR_CLIENT_ID=36 ;;
esac

mkdir -p logs
LOG_FILE="logs/${SKILL}.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting /${SKILL} (IBKR_CLIENT_ID=${IBKR_CLIENT_ID:-unset}, FLAG=${FLAG})" >> "$LOG_FILE"

# Preflight: verify IBKR clientId is free; non-fatal.
python scripts/ibkr_preflight.py >> "$LOG_FILE" 2>&1 || true

if [[ -n "$DEV_PREFIX" ]]; then
    PROMPT="${DEV_PREFIX} /${SKILL}"
else
    PROMPT="/${SKILL}"
fi

# Inner timeout: defends against a single Claude/API hang independent of the
# orchestrator's outer timeout. Lower than the orchestrator's per-skill cap so
# the inner kill is the one that fires first (cleaner exit + log line).
INNER_TIMEOUT_SEC="${SKILL_INNER_TIMEOUT_SEC:-840}"
timeout --foreground "$INNER_TIMEOUT_SEC" \
    claude --dangerously-skip-permissions --model claude-opus-4-7 -p "$PROMPT" >> "$LOG_FILE" 2>&1
EC=$?
if [[ $EC -eq 124 ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] /${SKILL} TIMED OUT after ${INNER_TIMEOUT_SEC}s" >> "$LOG_FILE"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Finished /${SKILL} (exit code ${EC})" >> "$LOG_FILE"
exit $EC
