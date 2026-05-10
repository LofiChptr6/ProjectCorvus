#!/usr/bin/env bash
# Hourly orchestrator — fans out 11 sector reviews + mike-allocator, then runs
# the desk heartbeat. Called once per hour by trading-hourly-review.timer.
#
# Phases (in order):
#   1a. Migrated sector reviews — atlas, commodity, energy, fab, fabless, iron,
#       maya, rex, trump, vera, volt — via the Python pipeline (scripts/run_skill.py).
#       This is the new path: bundler → template → vLLM → structured-output writes.
#   1b. Legacy harness skills (currently empty — titan was decommissioned in
#       favor of energy + commodity, which together cover its scope).
#   2.  Mike-allocator — harness path (writes live orders; sensitive).
#   3.  Hourly-review heartbeat — harness path.
#
# Skip-fast: during the AZ quiet window (22:00–05:00) or weekends we skip
# phases 1 and 2 and only run the heartbeat.
#
# Per-sub-skill logs land in logs/<skill>.log; the orchestrator's own
# coordination log is logs/hourly-orchestrator.log.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON_BIN:-${REPO_ROOT}/.venv/bin/python}"

mkdir -p logs
ORCH_LOG="logs/hourly-orchestrator.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$ORCH_LOG"; }

log "orchestrator start (pid=$$ python=$PYTHON)"

AZ_HOUR=$(TZ='America/Phoenix' date +%-H)
AZ_DOW=$(TZ='America/Phoenix' date +%u)

is_quiet=false
if [[ $AZ_HOUR -ge 22 || $AZ_HOUR -lt 5 ]]; then is_quiet=true; fi
is_weekend=false
if [[ $AZ_DOW -ge 6 ]]; then is_weekend=true; fi

if $is_quiet || $is_weekend; then
    log "skip fan-out (az_hour=$AZ_HOUR dow=$AZ_DOW quiet=$is_quiet weekend=$is_weekend); heartbeat only"
    bash "$SCRIPT_DIR/run_scheduled_skill.sh" hourly-review
    log "orchestrator end (heartbeat-only path)"
    exit 0
fi

CONCURRENCY="${ORCH_CONCURRENCY:-4}"
SKILL_TIMEOUT_SEC="${SKILL_TIMEOUT_SEC:-900}"

# 11 migrated sectors → Python pipeline.
PIPELINE_SECTORS=(atlas commodity energy fab fabless iron maya rex trump vera volt)
# Legacy harness skills decommissioned (titan superseded by energy + commodity).
# Keep .md files in .claude/commands/ for audit; orchestrator no longer fires.
HARNESS_SKILLS=()

log "phase 1a: ${#PIPELINE_SECTORS[@]} sectors via Python pipeline (concurrency=$CONCURRENCY)"

# Per-sector launcher wrapper so xargs can carry the timeout.
run_pipeline_sector() {
    local sector="$1"
    timeout --foreground "$SKILL_TIMEOUT_SEC" \
        "$PYTHON" "$SCRIPT_DIR/run_skill.py" "$sector" review \
        >> "logs/${sector}-review.log" 2>&1
}
export -f run_pipeline_sector
export PYTHON SCRIPT_DIR SKILL_TIMEOUT_SEC

printf '%s\n' "${PIPELINE_SECTORS[@]}" \
    | xargs -n1 -P "$CONCURRENCY" -I {} \
        bash -c 'run_pipeline_sector "$@"' _ {} \
    || log "phase 1a: one or more sector reviews exited non-zero or timed out (continuing)"

if [[ ${#HARNESS_SKILLS[@]} -gt 0 ]]; then
    log "phase 1b: legacy harness skills: ${HARNESS_SKILLS[*]}"
    for skill in "${HARNESS_SKILLS[@]}"; do
        timeout --foreground "$SKILL_TIMEOUT_SEC" \
            bash "$SCRIPT_DIR/run_scheduled_skill.sh" "$skill" \
            || log "phase 1b: $skill exited non-zero"
    done
fi

log "phase 2: running mike-allocator (harness)"
timeout --foreground "$SKILL_TIMEOUT_SEC" bash "$SCRIPT_DIR/run_scheduled_skill.sh" mike-allocator
ec=$?
if [[ $ec -ne 0 ]]; then log "phase 2: mike-allocator exited non-zero (exit=$ec, may be timeout 124)"; fi

log "phase 3: running hourly-review heartbeat (harness)"
timeout --foreground "$SKILL_TIMEOUT_SEC" bash "$SCRIPT_DIR/run_scheduled_skill.sh" hourly-review
ec=$?
if [[ $ec -ne 0 ]]; then log "phase 3: heartbeat exited non-zero (exit=$ec, may be timeout 124)"; fi

log "orchestrator end"
