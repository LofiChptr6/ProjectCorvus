#!/usr/bin/env bash
# Stop, disable, and remove every systemd user unit installed by
# scripts/install_schedules.sh. Idempotent.
set -euo pipefail

UNIT_DST="${HOME}/.config/systemd/user"

LONG_RUNNING=(
    trading-vllm
    trading-vllm-embed
    trading-llm-proxy
    trading-dashboard
)

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

for u in "${LONG_RUNNING[@]}"; do
    systemctl --user disable --now "${u}.service" 2>/dev/null || true
    rm -f "${UNIT_DST}/${u}.service"
    echo "Removed ${u}.service"
done

for u in "${UNITS[@]}"; do
    systemctl --user disable --now "${u}.timer"   2>/dev/null || true
    systemctl --user stop          "${u}.service" 2>/dev/null || true
    rm -f "${UNIT_DST}/${u}.service" "${UNIT_DST}/${u}.timer"
    echo "Removed ${u}.service / ${u}.timer"
done

systemctl --user daemon-reload
echo "Uninstalled."
