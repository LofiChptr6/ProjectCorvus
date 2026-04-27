#!/usr/bin/env bash
# Hourly orchestrator — fans out the 10 sector reviews + mike-allocator,
# then runs the desk heartbeat. Called once per hour by
# trading-hourly-review.timer.
#
# Phases (in order):
#   1. Sector reviews   — atlas, fab, fabless, rex, maya, titan, vera,
#                         trump, iron, volt    (parallel, concurrency=$ORCH_CONCURRENCY)
#   2. Mike-allocator    — reads the consolidated conviction stack and proposes
#                          (or in Stage 3, places) the rebalance orders
#   3. Hourly heartbeat — telegram inbox + desk-state ping
#
# Skip-fast: during the AZ quiet window (22:00–05:00) or on weekends we skip
# phases 1 and 2 entirely and only run the heartbeat. Each per-agent review
# self-skips when the market is closed, so the rest of the off-hours envelope
# (e.g. weekday 14:00–22:00 AZ) is handled cheaply by the agents themselves.
#
# Each sub-skill is launched via run_scheduled_skill.sh, which assigns a
# unique IBKR client id and tee's per-skill logs into logs/<skill>.log.
# This script's own coordination log is logs/hourly-orchestrator.log.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

mkdir -p logs
ORCH_LOG="logs/hourly-orchestrator.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$ORCH_LOG"; }

log "orchestrator start (pid=$$)"

# Phoenix: no DST. Quiet window = 22:00–05:00 local.
AZ_HOUR=$(TZ='America/Phoenix' date +%-H)
AZ_DOW=$(TZ='America/Phoenix' date +%u)   # 1=Mon … 7=Sun

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

# Concurrency cap for sector fan-out. 4 keeps RAM/CPU + Anthropic API
# concurrency reasonable while finishing well inside the hour even when each
# review takes ~10 min. Override with ORCH_CONCURRENCY env if needed.
CONCURRENCY="${ORCH_CONCURRENCY:-4}"

SECTORS=(atlas fab fabless rex maya titan vera trump iron volt)

log "phase 1: fanning out ${#SECTORS[@]} sector reviews (concurrency=$CONCURRENCY)"

# Per-skill timeout. A hung sector review otherwise holds an xargs slot for the
# whole 2700s systemd envelope; with concurrency=4 a few hangs would stall the
# entire hour. Override per-call via SKILL_TIMEOUT_SEC.
SKILL_TIMEOUT_SEC="${SKILL_TIMEOUT_SEC:-900}"

# xargs -P bounds parallelism. Each sub-launcher writes its own log; we don't
# capture per-skill stdout here. Failures in one agent must not abort the rest,
# so we ignore the xargs exit code (its non-zero exit is logged below).
printf '%s\n' "${SECTORS[@]}" \
    | xargs -n1 -P "$CONCURRENCY" -I {} \
        timeout --foreground "$SKILL_TIMEOUT_SEC" bash "$SCRIPT_DIR/run_scheduled_skill.sh" "{}-review" \
    || log "phase 1: one or more sector reviews exited non-zero or timed out (continuing)"

log "phase 2: running mike-allocator"
timeout --foreground "$SKILL_TIMEOUT_SEC" bash "$SCRIPT_DIR/run_scheduled_skill.sh" mike-allocator
ec=$?
if [[ $ec -ne 0 ]]; then log "phase 2: mike-allocator exited non-zero (exit=$ec, may be timeout 124)"; fi

log "phase 3: running hourly-review heartbeat"
timeout --foreground "$SKILL_TIMEOUT_SEC" bash "$SCRIPT_DIR/run_scheduled_skill.sh" hourly-review
ec=$?
if [[ $ec -ne 0 ]]; then log "phase 3: heartbeat exited non-zero (exit=$ec, may be timeout 124)"; fi

log "orchestrator end"
