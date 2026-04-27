#!/usr/bin/env bash
# Install cron entries for Claude Code trading skills on Linux/macOS.
# Idempotent: removes any prior entries tagged with #CLAUDE_TRADING before adding.
#
# Times are written in LOCAL TZ. The desk runs America/Phoenix (MST, no DST).
# Verify with `timedatectl` (Linux) or `date` — set the box's TZ before
# running this script:
#     sudo timedatectl set-timezone America/Phoenix
# If you keep the box on UTC, shift every entry below by +7 hours.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCHER="$REPO_ROOT/scripts/run_scheduled_skill.sh"
chmod +x "$LAUNCHER"

TAG="#CLAUDE_TRADING"

# (cron_expr, skill_name) — local time, Mon-Fri unless noted
ENTRIES=(
    "6 6 * * 1-5|mike-morning"
    "0 8 * * 1-5|mike-midday"
    "0 23 * * 1-5|cassidy-evening"
    "0 * * * 1-5|hourly-review"
    "30 6 * * 1-5|rex-review"
    "30 6 * * 1-5|maya-review"
    "30 6 * * 1-5|atlas-review"
    "30 6 * * 1-5|titan-review"
    "30 6 * * 1-5|fab-review"
    "30 6 * * 1-5|fabless-review"
    "30 6 * * 1-5|trump-review"
    "30 6 * * 1-5|vera-review"
    "30 6 * * 1-5|iron-review"
    "30 6 * * 1-5|volt-review"
    "30 6-13 * * 1-5|mike-allocator"
    "0 16 * * 1-5|rex-evening"
    "0 16 * * 1-5|maya-evening"
    "0 16 * * 1-5|atlas-evening"
    "0 16 * * 1-5|titan-evening"
    "0 16 * * 1-5|fab-evening"
    "0 16 * * 1-5|fabless-evening"
    "0 16 * * 1-5|trump-evening"
    "0 16 * * 1-5|vera-evening"
    "0 16 * * 1-5|iron-evening"
    "0 16 * * 1-5|volt-evening"
    "0 23 * * 6|sector-archivist"
)

# Strip prior entries
EXISTING="$(crontab -l 2>/dev/null | grep -v "$TAG" || true)"

NEW="$EXISTING"
for entry in "${ENTRIES[@]}"; do
    cron="${entry%%|*}"
    skill="${entry##*|}"
    NEW+=$'\n'"$cron $LAUNCHER $skill >/dev/null 2>&1 $TAG"
done

echo "$NEW" | crontab -
echo "Installed ${#ENTRIES[@]} cron entries (tagged $TAG)."
crontab -l | grep "$TAG"
