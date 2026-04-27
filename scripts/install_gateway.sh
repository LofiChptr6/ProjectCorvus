#!/usr/bin/env bash
# Install the Telegram → Claude Code gateway as a background service.
#   Linux: systemd user unit  (~/.config/systemd/user/telegram-gateway.service)
#   macOS: launchd LaunchAgent (~/Library/LaunchAgents/com.tianyi.telegram-gateway.plist)
#
# Idempotent — re-run any time. Safe to run from a fresh clone.

set -euo pipefail

# ── Resolve paths ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$( cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd )"
PROJECT_DIR="$( cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd )"
GATEWAY_PY="$SCRIPT_DIR/telegram_gateway.py"
ENV_FILE="$PROJECT_DIR/.env"
LOG_DIR="$PROJECT_DIR/logs"

mkdir -p "$LOG_DIR"

# ── OS detection ──────────────────────────────────────────────────────────────
case "$(uname -s)" in
  Linux)   PLATFORM=linux ;;
  Darwin)  PLATFORM=mac   ;;
  *) echo "❌ Unsupported OS: $(uname -s) (Linux and macOS only)"; exit 1 ;;
esac

echo "── Telegram → Claude Code gateway installer ──"
echo "Platform:    $PLATFORM"
echo "Project:     $PROJECT_DIR"

# ── Prerequisite: claude CLI ──────────────────────────────────────────────────
if ! CLAUDE_BIN="$(command -v claude)"; then
  echo
  echo "❌ 'claude' CLI not found on PATH."
  echo "   Install Claude Code first:  https://claude.com/code"
  exit 1
fi
echo "claude:      $CLAUDE_BIN"

# ── Prerequisite: python3 ─────────────────────────────────────────────────────
if   command -v python3 >/dev/null 2>&1; then PYTHON="$(command -v python3)"
elif command -v python  >/dev/null 2>&1; then PYTHON="$(command -v python)"
else
  echo "❌ python3 not found. Install Python 3.10+ first."
  exit 1
fi
echo "python:      $PYTHON"

# ── Prerequisite: terminal emulator (Linux only) ──────────────────────────────
if [ "$PLATFORM" = "linux" ]; then
  TERM_FOUND=""
  for t in ptyxis gnome-terminal konsole xfce4-terminal kitty alacritty wezterm xterm; do
    if command -v "$t" >/dev/null 2>&1; then TERM_FOUND="$t"; break; fi
  done
  if [ -z "$TERM_FOUND" ]; then
    echo "❌ No supported terminal emulator found."
    echo "   Install one of: ptyxis, gnome-terminal, konsole, xfce4-terminal, kitty, alacritty, wezterm, xterm"
    exit 1
  fi
  echo "terminal:    $TERM_FOUND"
fi

# ── Install Python deps ───────────────────────────────────────────────────────
echo
echo "→ Installing Python dependencies (httpx, python-dotenv)…"
"$PYTHON" -m pip install --user --quiet httpx python-dotenv

# ── Telegram credentials ──────────────────────────────────────────────────────
need_token=true; need_chat=true
if [ -f "$ENV_FILE" ]; then
  grep -q "^TELEGRAM_BOT_TOKEN=" "$ENV_FILE" && need_token=false
  grep -q "^TELEGRAM_CHAT_ID="   "$ENV_FILE" && need_chat=false
fi

if $need_token || $need_chat; then
  echo
  echo "→ Telegram credentials needed."
  echo "  How to get them:"
  echo "    1. Talk to @BotFather on Telegram → /newbot → save the HTTP API token"
  echo "    2. Send any message to your new bot"
  echo "    3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates → find your chat id"
  echo
  if $need_token; then
    read -r -p "  TELEGRAM_BOT_TOKEN: " TG_TOKEN
    echo "TELEGRAM_BOT_TOKEN=$TG_TOKEN" >> "$ENV_FILE"
  fi
  if $need_chat; then
    read -r -p "  TELEGRAM_CHAT_ID:   " TG_CHAT
    echo "TELEGRAM_CHAT_ID=$TG_CHAT" >> "$ENV_FILE"
  fi
  echo "  Saved to $ENV_FILE"
fi

# ── Install service ───────────────────────────────────────────────────────────
echo
case "$PLATFORM" in
  linux)
    UNIT_DIR="$HOME/.config/systemd/user"
    UNIT_FILE="$UNIT_DIR/telegram-gateway.service"
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Telegram → Claude Code gateway
After=network-online.target graphical-session.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$PYTHON $GATEWAY_PY
Restart=on-failure
RestartSec=5
StandardOutput=append:$LOG_DIR/gateway.log
StandardError=append:$LOG_DIR/gateway.err

[Install]
WantedBy=default.target
EOF
    echo "→ Wrote $UNIT_FILE"
    systemctl --user daemon-reload
    systemctl --user enable --now telegram-gateway.service
    # Run-on-boot even when not logged in
    if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
      echo "→ Enabling user lingering (run service even when logged out)…"
      sudo loginctl enable-linger "$USER" || \
        echo "  (skipped — service will only run while you are logged in)"
    fi
    echo
    echo "✅ Service installed and started."
    echo "   Status:    systemctl --user status telegram-gateway"
    echo "   Logs:      journalctl --user -fu telegram-gateway"
    echo "              tail -f $LOG_DIR/gateway.log"
    echo "   Stop:      systemctl --user stop telegram-gateway"
    echo "   Uninstall: $SCRIPT_DIR/uninstall_gateway.sh"
    ;;

  mac)
    LABEL="com.tianyi.telegram-gateway"
    PLIST_DIR="$HOME/Library/LaunchAgents"
    PLIST="$PLIST_DIR/$LABEL.plist"
    mkdir -p "$PLIST_DIR"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$GATEWAY_PY</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/gateway.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/gateway.err</string>
</dict>
</plist>
EOF
    echo "→ Wrote $PLIST"
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo
    echo "✅ LaunchAgent installed and started."
    echo "   Status:    launchctl list | grep telegram-gateway"
    echo "   Logs:      tail -f $LOG_DIR/gateway.log"
    echo "   Stop:      launchctl unload $PLIST"
    echo "   Uninstall: $SCRIPT_DIR/uninstall_gateway.sh"
    ;;
esac

echo
echo "📨  Send any text to your Telegram bot — a terminal will pop up running:"
echo "       claude --dangerously-skip-permissions \"<your message>\""
