#!/usr/bin/env bash
# Remove the Telegram → Claude Code gateway service.
set -euo pipefail

case "$(uname -s)" in
  Linux)
    UNIT="$HOME/.config/systemd/user/telegram-gateway.service"
    if [ -f "$UNIT" ]; then
      systemctl --user disable --now telegram-gateway.service 2>/dev/null || true
      rm -f "$UNIT"
      systemctl --user daemon-reload
      echo "✅ Removed $UNIT"
    else
      echo "ℹ️  No systemd unit found at $UNIT"
    fi
    ;;
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/com.tianyi.telegram-gateway.plist"
    if [ -f "$PLIST" ]; then
      launchctl unload "$PLIST" 2>/dev/null || true
      rm -f "$PLIST"
      echo "✅ Removed $PLIST"
    else
      echo "ℹ️  No LaunchAgent found at $PLIST"
    fi
    ;;
  *)
    echo "❌ Unsupported OS: $(uname -s)"; exit 1 ;;
esac
