#!/usr/bin/env bash
# Weekly model-tune orchestrator — fans out *-model-tune across all 11 sectors.
#
# Triggered by trading-weekly-tune.timer (Sundays, off-market hours). Each
# sector gets its own subprocess via xargs concurrency, mirroring the hourly
# orchestrator pattern. Use --dry-run to write file actions to the .shadow
# folder; default is live.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

SECTORS="${WEEKLY_TUNE_SECTORS:-atlas commodity energy fab fabless iron maya rex trump vera volt}"
CONCURRENCY="${WEEKLY_TUNE_CONCURRENCY:-3}"
DRY_RUN_FLAG="${WEEKLY_TUNE_DRY_RUN:-}"
PER_SKILL_TIMEOUT="${WEEKLY_TUNE_TIMEOUT_SEC:-1500}"

mkdir -p logs
LOG="logs/weekly-tune-orchestrator.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting weekly model-tune cycle (sectors=${SECTORS} dry_run=${DRY_RUN_FLAG:-no})" >> "$LOG"

PYTHON="${PYTHON_BIN:-${REPO_ROOT}/.venv/bin/python}"

run_one() {
    local sector="$1"
    if [[ -n "$DRY_RUN_FLAG" ]]; then
        # No --dry-run flag on run_skill.py yet — set env var.
        WEEKLY_TUNE_DRY_RUN=1 timeout "$PER_SKILL_TIMEOUT" "$PYTHON" \
            scripts/run_skill.py "$sector" model_tune \
            >> "logs/${sector}-model-tune.log" 2>&1
    else
        timeout "$PER_SKILL_TIMEOUT" "$PYTHON" \
            scripts/run_skill.py "$sector" model_tune \
            >> "logs/${sector}-model-tune.log" 2>&1
    fi
}
export -f run_one
export DRY_RUN_FLAG PER_SKILL_TIMEOUT REPO_ROOT PYTHON

echo "$SECTORS" | tr ' ' '\n' | xargs -n1 -P "$CONCURRENCY" -I{} bash -c 'run_one "$@"' _ {}
EC=$?

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Weekly cycle done (exit ${EC})" >> "$LOG"
exit $EC
