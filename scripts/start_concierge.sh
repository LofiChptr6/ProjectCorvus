#!/usr/bin/env bash
# Launch the Sonnet-backed Telegram concierge in the foreground (Linux/macOS).
#
# Prereqs:
#   - python3 on PATH (matches the one used for the MCP server)
#   - `pip install -r requirements.txt` including anthropic>=0.40.0
#   - .env contains ANTHROPIC_API_KEY and TELEGRAM_BOT_TOKEN
#
# Logs: logs/concierge.log
# Stop: Ctrl-C. State is cleaned up on graceful shutdown.
#
# For background / always-on use, prefer the systemd unit:
#   scripts/concierge.service
# See concierge/README.md "Linux modes" for setup.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

export IBKR_CLIENT_ID="${IBKR_CLIENT_ID:-2}"

# Prefer .venv if present, otherwise fall back to system python3.
PYTHON_BIN="python3"
if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
fi

exec "$PYTHON_BIN" -m concierge.service
