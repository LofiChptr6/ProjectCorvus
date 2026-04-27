#!/usr/bin/env bash
# Install the four director-routine systemd USER timers:
#   trading-hourly-review     (every hour, quiet-window gated inside the command)
#   trading-mike-morning      (weekdays 9:06 ET)
#   trading-mike-midday       (weekdays 11:00 ET)
#   trading-cassidy-evening   (weekdays 23:00 Phoenix)
#
# Each timer fires its matching .service, which runs:
#   claude -p "/<command-name>" --dangerously-skip-permissions
# from the project working directory. The slash commands are defined in
# .claude/commands/*.md and use the ibkr-trading MCP server (.mcp.json).
#
# Idempotent: re-running this script just refreshes the unit files, then
# re-enables the timers. Safe to run on every deploy / config change.
#
# Per-agent routines (morning_scan, monitor_fills, daily_summary,
# close_positions) are NOT installed by this script — those are Python
# scripts in agent/routines/ that the YAML files schedule per agent, and
# need a dispatcher that doesn't exist yet. See INSTALL.md.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="${REPO_ROOT}/scripts/systemd"
UNIT_DST="${HOME}/.config/systemd/user"

UNITS=(
    trading-hourly-review
    trading-mike-morning
    trading-mike-midday
    trading-cassidy-evening
)

# Sanity: claude CLI present?
if ! command -v claude >/dev/null 2>&1; then
    echo "ERROR: 'claude' CLI not found in PATH. Install Claude Code before running this." >&2
    exit 1
fi
CLAUDE_BIN="$(command -v claude)"
if [[ "$CLAUDE_BIN" != "/home/tianyizhang/.local/bin/claude" ]]; then
    echo "WARNING: claude is at $CLAUDE_BIN — the unit files hardcode /home/tianyizhang/.local/bin/claude." >&2
    echo "         Edit scripts/systemd/*.service to match if needed before continuing." >&2
fi

mkdir -p "$UNIT_DST" "${REPO_ROOT}/logs"

echo "Installing units to $UNIT_DST ..."
for u in "${UNITS[@]}"; do
    install -m 0644 "${UNIT_SRC}/${u}.service" "${UNIT_DST}/${u}.service"
    install -m 0644 "${UNIT_SRC}/${u}.timer"   "${UNIT_DST}/${u}.timer"
    echo "  + ${u}.service / ${u}.timer"
done

systemctl --user daemon-reload

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
