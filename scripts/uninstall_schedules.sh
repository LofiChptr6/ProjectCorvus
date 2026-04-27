#!/usr/bin/env bash
# Stop, disable, and remove the four director-routine systemd user timers.
set -euo pipefail

UNIT_DST="${HOME}/.config/systemd/user"
UNITS=(
    trading-hourly-review
    trading-mike-morning
    trading-mike-midday
    trading-cassidy-evening
)

for u in "${UNITS[@]}"; do
    systemctl --user disable --now "${u}.timer"   2>/dev/null || true
    systemctl --user stop          "${u}.service" 2>/dev/null || true
    rm -f "${UNIT_DST}/${u}.service" "${UNIT_DST}/${u}.timer"
    echo "Removed ${u}.service / ${u}.timer"
done

systemctl --user daemon-reload
echo "Uninstalled."
