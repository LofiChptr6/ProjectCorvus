#!/usr/bin/env bash
# Generic launcher for Claude Code scheduled skills.
# Called by the systemd .service units and by the orchestrator scripts:
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

# IBKR connections are now multiplexed through ibkr-daemon.service (one
# clientId=1 connection at 127.0.0.1:7790). Skills no longer need their own
# clientIds; the per-skill case block was removed 2026-04-28 with the daemon
# refactor. See ibkr/daemon.py.

mkdir -p logs
LOG_FILE="logs/${SKILL}.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting /${SKILL} (FLAG=${FLAG})" >> "$LOG_FILE"

if [[ -n "$DEV_PREFIX" ]]; then
    PROMPT="${DEV_PREFIX} /${SKILL}"
else
    PROMPT="/${SKILL}"
fi

# Inner timeout: defends against a single Claude/LLM-proxy hang independent of
# the orchestrator's outer timeout. Lower than the orchestrator's per-skill cap
# so the inner kill is the one that fires first (cleaner exit + log line).
INNER_TIMEOUT_SEC="${SKILL_INNER_TIMEOUT_SEC:-840}"

# Generate a per-invocation session_id and route through obs/proxy.py at port 8001.
# The proxy strips the /skill/<name>/session/<uuid> prefix, forwards to vLLM at
# port 8000, and tees every exchange into Postgres audit_log + tool_calls so the
# Streamlit dashboard at :8501 can render live agent activity.
#
# If the proxy is down, fall back to vLLM directly so the desk keeps trading
# even if obs is broken — observability is non-critical to execution.
SESSION_ID=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())')
PROXY_BASE="${LLM_PROXY_URL:-http://localhost:8001}"
DEFAULT_BASE="${PROXY_BASE}/skill/${SKILL}/session/${SESSION_ID}"
if ! curl -fsS --max-time 1 "${PROXY_BASE}/healthz" >/dev/null 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] obs proxy at ${PROXY_BASE} unreachable; bypassing to vLLM" >> "$LOG_FILE"
    DEFAULT_BASE="${LOCAL_LLM_FALLBACK_URL:-http://localhost:8000}"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] session_id=${SESSION_ID}" >> "$LOG_FILE"

ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-${DEFAULT_BASE}}" \
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-local-dummy}" \
timeout --foreground "$INNER_TIMEOUT_SEC" \
    claude --dangerously-skip-permissions -p "$PROMPT" >> "$LOG_FILE" 2>&1
EC=$?
if [[ $EC -eq 124 ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] /${SKILL} TIMED OUT after ${INNER_TIMEOUT_SEC}s" >> "$LOG_FILE"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Finished /${SKILL} (exit code ${EC})" >> "$LOG_FILE"
exit $EC
