#!/usr/bin/env bash
# Evening orchestrator — fans out 11 sector evenings via the Python pipeline.
# Called by trading-sector-evenings.timer at 20:00 AZ.
#
# Cassidy is NOT fired from here anymore (was firing twice per evening —
# once at 20:00 via this script's phase 2, again at 22:00 via its dedicated
# timer). The dedicated timer is the single canonical Cassidy invocation;
# each per-sector evening still writes its digest before exit so Cassidy
# can aggregate them when she runs.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON_BIN:-${REPO_ROOT}/.venv/bin/python}"

mkdir -p logs
ORCH_LOG="logs/evening-orchestrator.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$ORCH_LOG"; }

log "evening-orchestrator start (pid=$$ python=$PYTHON)"

CONCURRENCY="${EVENING_CONCURRENCY:-3}"
SKILL_TIMEOUT_SEC="${SKILL_TIMEOUT_SEC:-1200}"

PIPELINE_SECTORS=(atlas commodity energy fab fabless iron maya rex trump vera volt)
HARNESS_SKILLS=()

log "phase 1a: ${#PIPELINE_SECTORS[@]} sector evenings via Python pipeline"

run_pipeline_evening() {
    local sector="$1"
    timeout --foreground "$SKILL_TIMEOUT_SEC" \
        "$PYTHON" "$SCRIPT_DIR/run_skill.py" "$sector" evening \
        >> "logs/${sector}-evening.log" 2>&1
}
export -f run_pipeline_evening
export PYTHON SCRIPT_DIR SKILL_TIMEOUT_SEC

printf '%s\n' "${PIPELINE_SECTORS[@]}" \
    | xargs -n1 -P "$CONCURRENCY" -I {} \
        bash -c 'run_pipeline_evening "$@"' _ {} \
    || log "phase 1a: one or more sector evenings exited non-zero or timed out (continuing)"

if [[ ${#HARNESS_SKILLS[@]} -gt 0 ]]; then
    log "phase 1b: legacy harness evenings: ${HARNESS_SKILLS[*]}"
    for skill in "${HARNESS_SKILLS[@]}"; do
        timeout --foreground "$SKILL_TIMEOUT_SEC" \
            bash "$SCRIPT_DIR/run_scheduled_skill.sh" "$skill" \
            || log "phase 1b: $skill exited non-zero"
    done
fi

log "evening-orchestrator end (cassidy fires separately at 22:00 via her own timer)"
