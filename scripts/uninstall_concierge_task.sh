#!/usr/bin/env bash
# Stop, disable, and remove the per-user trading-concierge unit.
set -euo pipefail

UNIT_DST="${HOME}/.config/systemd/user/trading-concierge.service"

if [[ ! -f "$UNIT_DST" ]]; then
    echo "$UNIT_DST not present — nothing to uninstall."
    exit 0
fi

systemctl --user stop trading-concierge.service 2>/dev/null || true
systemctl --user disable trading-concierge.service 2>/dev/null || true
rm -f "$UNIT_DST"
systemctl --user daemon-reload
echo "Per-user trading-concierge unit removed."
