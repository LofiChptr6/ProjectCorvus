#!/usr/bin/env bash
# Mode C — install concierge as a per-user systemd unit (Linux port of
# install_concierge_task.bat). Auto-starts when the user logs in.
#
# By default this is "active session only" — closes when the user logs out.
# To make it survive logout / reboot without re-login (closer to Mode D
# without sudo), enable lingering:
#     loginctl enable-linger $(whoami)
# That setting persists across reboots.
#
# After install:
#   systemctl --user start  trading-concierge
#   systemctl --user status trading-concierge
#   journalctl --user -u trading-concierge -f
#
# Uninstall: scripts/uninstall_concierge_task.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="${REPO_ROOT}/scripts/systemd/trading-concierge.user.service"
UNIT_DIR="${HOME}/.config/systemd/user"
UNIT_DST="${UNIT_DIR}/trading-concierge.service"

if [[ ! -f "$UNIT_SRC" ]]; then
    echo "ERROR: missing $UNIT_SRC" >&2
    exit 1
fi
if [[ ! -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    echo "ERROR: .venv not initialized. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

REPO_URL_PATH="$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$REPO_ROOT")"

mkdir -p "$UNIT_DIR"
sed \
    -e "s|@REPO_PATH@|${REPO_ROOT}|g" \
    -e "s|@REPO_URL_PATH@|${REPO_URL_PATH}|g" \
    -e "s|@USER@|${USER}|g" \
    "$UNIT_SRC" > "$UNIT_DST"
chmod 0644 "$UNIT_DST"
systemctl --user daemon-reload
systemctl --user enable trading-concierge.service

echo
echo "User service installed and enabled at login."
echo "Start now:        systemctl --user start trading-concierge"
echo "Tail logs:        journalctl --user -u trading-concierge -f"
echo "Survive logout:   loginctl enable-linger $(whoami)"
echo "Uninstall:        scripts/uninstall_concierge_task.sh"
