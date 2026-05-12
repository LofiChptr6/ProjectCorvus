#!/usr/bin/env bash
# Install the systemd USER timers and long-running services that drive the desk.
#
# Source-of-truth unit files in scripts/systemd/ use placeholder tokens
# (@REPO_PATH@, @USER@, @CLAUDE_BIN@, @CLAUDE_BIN_DIR@, @REPO_URL_PATH@) so the
# repo is portable across machines. This script renders them with the current
# environment and installs into ~/.config/systemd/user/.
#
# Idempotent: re-running just refreshes the rendered units + re-enables timers.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="${REPO_ROOT}/scripts/systemd"
UNIT_DST="${HOME}/.config/systemd/user"

# Long-running services (no timer pair).
# trading-vllm        — vLLM serving the local LLM (local-llm branch only)
# trading-vllm-embed  — vLLM embeddings server (local-llm branch only)
# trading-llm-proxy   — obs/proxy.py: captures /v1/messages into Postgres + live SSE
# trading-dashboard   — obs/dashboard.py: Streamlit multi-agent dashboard
LONG_RUNNING=(
    trading-vllm
    trading-vllm-embed
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
)

# --- Resolve placeholders ----------------------------------------------------

# claude CLI: required for every director skill (mike-*, cassidy-evening).
if ! command -v claude >/dev/null 2>&1; then
    echo "ERROR: 'claude' CLI not found in PATH." >&2
    echo "       Install Claude Code first: https://docs.anthropic.com/en/docs/claude-code/quickstart" >&2
    exit 1
fi
CLAUDE_BIN="$(command -v claude)"
CLAUDE_BIN_DIR="$(dirname "$CLAUDE_BIN")"

# URL-encoded repo path for `Documentation=file://` lines (spaces → %20).
REPO_URL_PATH="$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$REPO_ROOT")"

echo "Rendering systemd units with:"
echo "  REPO_PATH      = $REPO_ROOT"
echo "  USER           = $USER"
echo "  CLAUDE_BIN     = $CLAUDE_BIN"
echo "  CLAUDE_BIN_DIR = $CLAUDE_BIN_DIR"
echo "  REPO_URL_PATH  = $REPO_URL_PATH"
echo

# --- Optional sanity checks --------------------------------------------------

# Local-LLM venv: only needed if you're on the local-llm branch.
if [[ ! -x "${REPO_ROOT}/.venv-vllm/bin/vllm" ]]; then
    echo "NOTE: ${REPO_ROOT}/.venv-vllm/bin/vllm missing — trading-vllm will fail to start." >&2
    echo "      Skip if you're on 'main'. To bootstrap on 'local-llm':" >&2
    echo "        python3.12 -m venv .venv-vllm && .venv-vllm/bin/pip install vllm openai" >&2
fi

# Main venv: every other service needs it.
if [[ ! -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    echo "ERROR: ${REPO_ROOT}/.venv/bin/python missing — services that depend on the main venv will fail." >&2
    echo "       Bootstrap with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

mkdir -p "$UNIT_DST" "${REPO_ROOT}/logs"

# --- Render + install --------------------------------------------------------

render() {
    # Usage: render <src> <dst>
    local src="$1" dst="$2"
    sed \
        -e "s|@REPO_PATH@|${REPO_ROOT}|g" \
        -e "s|@REPO_URL_PATH@|${REPO_URL_PATH}|g" \
        -e "s|@USER@|${USER}|g" \
        -e "s|@CLAUDE_BIN@|${CLAUDE_BIN}|g" \
        -e "s|@CLAUDE_BIN_DIR@|${CLAUDE_BIN_DIR}|g" \
        "$src" > "$dst"
    chmod 0644 "$dst"
}

echo "Installing units to $UNIT_DST ..."
for u in "${LONG_RUNNING[@]}"; do
    if [[ ! -f "${UNIT_SRC}/${u}.service" ]]; then
        echo "  ! ${u}.service missing in $UNIT_SRC — skipped" >&2
        continue
    fi
    render "${UNIT_SRC}/${u}.service" "${UNIT_DST}/${u}.service"
    echo "  + ${u}.service (long-running)"
done
for u in "${UNITS[@]}"; do
    render "${UNIT_SRC}/${u}.service" "${UNIT_DST}/${u}.service"
    render "${UNIT_SRC}/${u}.timer"   "${UNIT_DST}/${u}.timer"
    echo "  + ${u}.service / ${u}.timer"
done

systemctl --user daemon-reload

# --- Enable -----------------------------------------------------------------

# Long-running: only enable those whose runtime is present.
echo "Enabling long-running services ..."
for u in "${LONG_RUNNING[@]}"; do
    case "$u" in
        trading-vllm|trading-vllm-embed)
            if [[ ! -x "${REPO_ROOT}/.venv-vllm/bin/vllm" ]]; then
                echo "  - ${u} (skipped: .venv-vllm not bootstrapped)"
                continue
            fi
            ;;
    esac
    systemctl --user enable --now "${u}.service" 2>&1 | sed 's/^/    /'
done

echo "Enabling timers ..."
for u in "${UNITS[@]}"; do
    systemctl --user enable --now "${u}.timer" 2>&1 | sed 's/^/    /'
done

# Lingering: without it, user services stop on logout. Required for 24/7 desk.
if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
    cat <<EOF

NOTE: linger is currently disabled. User timers will stop when you log out.
For an always-on trading desk, enable linger so services keep running:

    sudo loginctl enable-linger "$USER"

EOF
fi

echo
echo "Installed. Status:"
systemctl --user list-timers --all 2>/dev/null | grep -E "trading-|NEXT" | head -20 || true
echo
echo "Manual fire (test one):"
echo "  systemctl --user start trading-hourly-review.service"
echo "Tail a log:"
echo "  journalctl --user -u trading-mike-morning.service -f"
echo "Disable:"
echo "  scripts/uninstall_schedules.sh"
