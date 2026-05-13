#!/usr/bin/env bash
# Install the systemd USER timers that drive the desk:
#   trading-hourly-review     (every hour — orchestrator fans out 11 sector
#                              reviews via Python pipeline + mike-allocator
#                              on harness + heartbeat)
#   trading-mike-morning      (weekdays 9:06 ET)
#   trading-mike-midday       (weekdays 11:00 ET)
#   trading-cassidy-evening   (weekdays 23:00 Phoenix)
#   trading-sector-evenings   (weekdays 20:00 Phoenix — orchestrator fans
#                              out 11 sector-evening attribution reviews)
#   trading-weekly-tune       (Sundays 13:00 UTC — weekly model-tune
#                              cycle across 11 sector model portfolios)
#
# Sector review/evening/model_tune skills run via the Python pipeline
# (scripts/run_skill.py). Director skills (mike-{morning,midday,allocator},
# cassidy-evening, hourly-review heartbeat, titan legacy) still go through
# the Claude Code harness via scripts/run_scheduled_skill.sh.
#
# Idempotent: re-running this script just refreshes the unit files, then
# re-enables the timers. Safe to run on every deploy / config change.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="${REPO_ROOT}/scripts/systemd"
UNIT_DST="${HOME}/.config/systemd/user"

# Long-running services (no timer pair).
# trading-vllm        — vLLM serving Qwen3-32B-FP8 (Anthropic + OpenAI shapes natively)
# trading-llm-proxy   — obs/proxy.py: captures /v1/messages into Postgres + live SSE
# trading-dashboard   — obs/dashboard.py: Streamlit multi-agent dashboard
LONG_RUNNING=(
    trading-vllm
    trading-llm-proxy
    trading-dashboard
)

# Timer-driven units (each has a .service + matching .timer).
UNITS=(
    trading-hourly-review
    trading-mike-morning
    trading-mike-midday
    trading-cassidy-evening
    trading-sector-evenings
    trading-news-ingest
    trading-refresh-agent-state
    trading-weekly-tune
    trading-forecast-resolver
    trading-thesis-resolver
)

# Sanity: claude CLI present?
if ! command -v claude >/dev/null 2>&1; then
    echo "ERROR: 'claude' CLI not found in PATH. Install Claude Code before running this." >&2
    exit 1
fi

# Sanity: local-LLM venv present? (vLLM + litellm depend on it)
if [[ ! -x "${REPO_ROOT}/.venv-vllm/bin/vllm" ]]; then
    echo "WARNING: ${REPO_ROOT}/.venv-vllm/bin/vllm missing — trading-vllm will fail to start." >&2
    echo "         Bootstrap with: python3.12 -m venv .venv-vllm && .venv-vllm/bin/pip install vllm 'litellm[proxy]'" >&2
fi
CLAUDE_BIN="$(command -v claude)"
if [[ "$CLAUDE_BIN" != "/home/tianyizhang/.local/bin/claude" ]]; then
    echo "WARNING: claude is at $CLAUDE_BIN — the unit files hardcode /home/tianyizhang/.local/bin/claude." >&2
    echo "         Edit scripts/systemd/*.service to match if needed before continuing." >&2
fi

mkdir -p "$UNIT_DST" "${REPO_ROOT}/logs"

echo "Installing units to $UNIT_DST ..."
for u in "${LONG_RUNNING[@]}"; do
    install -m 0644 "${UNIT_SRC}/${u}.service" "${UNIT_DST}/${u}.service"
    echo "  + ${u}.service (long-running)"
done
for u in "${UNITS[@]}"; do
    install -m 0644 "${UNIT_SRC}/${u}.service" "${UNIT_DST}/${u}.service"
    install -m 0644 "${UNIT_SRC}/${u}.timer"   "${UNIT_DST}/${u}.timer"
    echo "  + ${u}.service / ${u}.timer"
done

systemctl --user daemon-reload

echo "Enabling long-running LLM services ..."
for u in "${LONG_RUNNING[@]}"; do
    systemctl --user enable --now "${u}.service"
done

echo "Enabling and starting timers ..."
for u in "${UNITS[@]}"; do
    systemctl --user enable --now "${u}.timer"
done

# Lingering: without it, user services stop on logout. Required for 24/7 desk.
if loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=no'; then
    cat <<'EOF'

NOTE: linger is currently disabled. User timers will stop when you log out.
For an always-on trading desk, enable linger so services keep running:

    sudo loginctl enable-linger "$USER"

EOF
fi

echo
echo "Installed. Status:"
systemctl --user list-timers --all | grep -E "trading-|NEXT" | head -10
echo
echo "Logs:"
for u in "${UNITS[@]}"; do
    echo "  $REPO_ROOT/logs/${u#trading-}.log"
done
echo
echo "Manual fire (test one):"
echo "  systemctl --user start trading-hourly-review.service"
echo "Disable:"
echo "  scripts/uninstall_schedules.sh"
