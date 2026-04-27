#!/usr/bin/env bash
# Stop, disable, and remove the trading-concierge system service.
set -euo pipefail

UNIT_DST="/etc/systemd/system/trading-concierge.service"

if [[ ! -f "$UNIT_DST" ]]; then
    echo "$UNIT_DST not present — nothing to uninstall."
    exit 0
fi

sudo systemctl stop trading-concierge.service 2>/dev/null || true
sudo systemctl disable trading-concierge.service 2>/dev/null || true
sudo rm -f "$UNIT_DST"
sudo systemctl daemon-reload
echo "trading-concierge service removed."
