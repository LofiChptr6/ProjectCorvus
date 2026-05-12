#!/usr/bin/env bash
# Mode D — install concierge as a system-wide systemd service. Runs at boot,
# auto-restarts on crash, survives logout. Requires sudo.
#
# Renders scripts/systemd/trading-concierge.service (which contains
# placeholders) with the current repo path + user before installing.
#
# After install:
#   sudo systemctl start  trading-concierge
#   sudo systemctl status trading-concierge
#   journalctl -u trading-concierge -f
#
# Uninstall: scripts/uninstall_concierge_service.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="${REPO_ROOT}/scripts/systemd/trading-concierge.service"
UNIT_DST="/etc/systemd/system/trading-concierge.service"
TMP_RENDERED="$(mktemp)"
trap 'rm -f "$TMP_RENDERED"' EXIT

if [[ ! -f "$UNIT_SRC" ]]; then
    echo "ERROR: missing $UNIT_SRC" >&2
    exit 1
fi
if [[ ! -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    echo "ERROR: .venv not initialized. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

REPO_URL_PATH="$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$REPO_ROOT")"
sed \
    -e "s|@REPO_PATH@|${REPO_ROOT}|g" \
    -e "s|@REPO_URL_PATH@|${REPO_URL_PATH}|g" \
    -e "s|@USER@|${USER}|g" \
    "$UNIT_SRC" > "$TMP_RENDERED"

echo "Installing $UNIT_DST (requires sudo)..."
sudo install -m 0644 "$TMP_RENDERED" "$UNIT_DST"
sudo systemctl daemon-reload
sudo systemctl enable trading-concierge.service

echo
echo "Service installed and enabled at boot."
echo "Start now:    sudo systemctl start trading-concierge"
echo "Tail logs:    journalctl -u trading-concierge -f"
echo "Stop:         sudo systemctl stop trading-concierge"
echo "Uninstall:    scripts/uninstall_concierge_service.sh"
