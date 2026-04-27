# Telegram → Claude Code Gateway

Replaces `opus trading/concierge` as the chat-ops front-end for the IKA bot
(`@IbkrTradingAgentBot`). Every text message you send to the bot opens a new
terminal window running `claude --dangerously-skip-permissions <your message>`
in the routed project directory. When Claude finishes, its final reply is
posted back to the same Telegram chat via a Stop hook.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                                Telegram                                      │
│                @IbkrTradingAgentBot   (token in .env, not committed)         │
└──────────────────────────────────────────────────────────────────────────────┘
                  ▲                                       ▲
                  │ getUpdates (long-poll)                │ sendMessage
                  │                                       │ (ack + Stop hook reply)
                  ▼                                       │
┌──────────────────────────────────────────────────────────────────────────────┐
│  systemd --user: telegram-gateway.service                                    │
│  python3 /home/tianyizhang/trading/scripts/telegram_gateway.py               │
│                                                                              │
│   1. _skip_backlog()       skip unread messages on startup                   │
│   2. poll loop             chat-ID filter (only TELEGRAM_CHAT_ID)            │
│   3. _route(text)          parse "[label] msg" prefix                        │
│        ├─ [trading] → /home/tianyizhang/trading        (default)             │
│        ├─ [parrot]  → /home/tianyizhang/AI Projects/ProjectParrot            │
│        └─ no prefix → default (trading)                                      │
│   4. launch_claude()       spawn terminal w/ TELEGRAM_GATEWAY_SESSION=1      │
│   5. ack                   "🚀 [trading] Launched: <prompt>"                  │
└──────────────────────────────────────────────────────────────────────────────┘
                                │ Popen
                                ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  ptyxis (or first available terminal: gnome-terminal, konsole, …)            │
│  bash -c "cd <workdir> && export TELEGRAM_GATEWAY_SESSION=1 &&               │
│           claude --dangerously-skip-permissions '<prompt>'; exec bash"       │
└──────────────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  Claude Code session  (OAuth — uses your Pro/Max subscription, no API key)   │
│  - reads project CLAUDE.md, MCP servers, hooks                               │
│  - writes JSONL transcript to ~/.claude/projects/<slug>/<uuid>.jsonl         │
└──────────────────────────────────────────────────────────────────────────────┘
                                │  Stop event
                                ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  Stop hook: scripts/stop_hook_telegram.py                                    │
│  registered in <project>/.claude/settings.json                               │
│                                                                              │
│   1. self-gate            exit early unless TELEGRAM_GATEWAY_SESSION=1       │
│   2. read transcript      walk JSONL backwards → last assistant text         │
│   3. send to Telegram     "💬 [trading]\n\n<reply>"                          │
│                                                                              │
│  Always exits 0 — never blocks Claude.                                       │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Files

| Path                                                       | Role                                  |
|------------------------------------------------------------|---------------------------------------|
| `scripts/telegram_gateway.py`                              | Long-poll loop + terminal launcher    |
| `scripts/stop_hook_telegram.py`                            | Posts final reply back to Telegram    |
| `scripts/install_gateway.sh`                               | One-shot installer (Linux + macOS)    |
| `scripts/uninstall_gateway.sh`                             | Service teardown                      |
| `.env`                                                     | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (mode 600) |
| `.claude/settings.json`                                    | Registers Stop hook for trading repo  |
| `~/.claude/projects/.../ProjectParrot/.claude/settings.json` | Same hook for parrot routing        |
| `~/.config/systemd/user/telegram-gateway.service`          | Service unit (Linux)                  |
| `~/Library/LaunchAgents/com.tianyi.telegram-gateway.plist` | LaunchAgent (macOS)                   |
| `logs/gateway.log`, `logs/gateway.err`                     | Service stdout/stderr                 |

## Routing

```
[trading] check positions          → cwd = /home/tianyizhang/trading
[parrot]  what is mocha doing      → cwd = /home/tianyizhang/AI Projects/ProjectParrot
no prefix here                     → cwd = /home/tianyizhang/trading (default)
```

Labels are case-insensitive. Unknown labels (`[foo] …`) fall through to the
default — the literal `[foo]` stays in the prompt, so Claude sees what you
typed.

To add a project, edit `PROJECTS` in `scripts/telegram_gateway.py`:

```python
PROJECTS = {
    "trading": "/home/tianyizhang/trading",
    "parrot":  "/home/tianyizhang/AI Projects/ProjectParrot",
    "default": DEFAULT_WORKDIR,
    # "foo": "/home/tianyizhang/foo",
}
```

Then `systemctl --user restart telegram-gateway`.

## Operating

| Action       | Command                                                         |
|--------------|-----------------------------------------------------------------|
| Status       | `systemctl --user status telegram-gateway`                      |
| Logs (live)  | `journalctl --user -fu telegram-gateway`                        |
| Logs (file)  | `tail -f /home/tianyizhang/trading/logs/gateway.log`            |
| Restart      | `systemctl --user restart telegram-gateway`                     |
| Stop         | `systemctl --user stop telegram-gateway`                        |
| Uninstall    | `bash scripts/uninstall_gateway.sh`                             |
| Run-on-boot  | `sudo loginctl enable-linger tianyizhang`  (one-time, optional) |

`enable-linger` lets the service run even when you're not logged in. Without
it, the gateway only runs while you have an active session.

## Security model

- **Single chat ACL.** Messages from chats other than `TELEGRAM_CHAT_ID`
  (set in `.env`) are dropped + logged.
- **Backlog skip on startup.** Old unread messages don't replay after restart.
- **No Anthropic API key.** Gateway only reads `TELEGRAM_BOT_TOKEN` /
  `TELEGRAM_CHAT_ID`. The spawned `claude` CLI authenticates via its own OAuth
  session, so all LLM cost is on the Pro/Max subscription, not metered API.
- **Stop hook self-gates** on `TELEGRAM_GATEWAY_SESSION=1`. Manually-started
  `claude` sessions in the same projects do **not** post their replies to
  Telegram — only gateway-launched ones do.

## Trade-offs vs. concierge (read this)

The gateway gives Claude Code the *full* MCP toolset of whatever project it
runs in. In `/home/tianyizhang/trading/`, that includes `place_order`,
`cancel_order`, `activate_kill_switch`, etc.

| Concern                          | concierge                                          | gateway                                             |
|----------------------------------|----------------------------------------------------|-----------------------------------------------------|
| Telegram poller                  | API-key + Sonnet via REST                          | OAuth subscription via spawned `claude` CLI         |
| Daily $ cap                      | yes (`CONCIERGE_DAILY_USD_CAP`)                    | n/a (subscription)                                  |
| Allowed-tools whitelist          | yes — read-only + propose                          | none — full MCP surface                             |
| `place_order` exposed?           | **no** (intentional)                               | **yes** (via MCP, no extra gate)                    |
| Approval gate for writes         | YES-confirm flow, staged intents                   | none in the gateway path                            |
| Conversation memory              | rolling history (40 turns)                         | each message = fresh `claude` session               |
| Slash commands (`/status`, `/pnl`) | yes (LLM-free, instant)                          | no                                                  |
| Reply path                       | in-thread Sonnet reply                             | Stop hook posts final assistant message             |
| Reliability                      | crashes → restart by hand                          | systemd `Restart=on-failure`                        |

You explicitly accepted the safety regression when greenlighting the swap. If
that ever stops feeling right, the simplest mitigation is to wire a Telegram
approval prompt into `place_order` itself (in
`tools/order.py` / `mcp_server.py`) so the gate lives at the tool boundary
rather than the chat boundary.

## Quick verify

```bash
systemctl --user is-active telegram-gateway     # → active
curl -s "https://api.telegram.org/bot$(grep TELEGRAM_BOT_TOKEN .env | cut -d= -f2)/getMe" | python3 -m json.tool
# → "username": "IbkrTradingAgentBot"
```

Then send any message to `@IbkrTradingAgentBot` from your Telegram.
A new ptyxis window opens at `/home/tianyizhang/trading`, you get a
🚀 launch ack, and when Claude finishes you get a 💬 reply.

## What was changed during the swap

| Before                                              | After                                            |
|-----------------------------------------------------|--------------------------------------------------|
| `concierge.service` running from `~/opus trading/`  | killed (PID 2045458), lock file removed          |
| IKA token only in `~/opus trading/.env`             | also in `~/trading/.env` (mode 600)              |
| No systemd unit for the IKA poller                  | `telegram-gateway.service` (enabled)             |
| Stop replies via concierge in-thread                | Stop hook posts last assistant message           |
| One project (trading)                               | `[trading]` and `[parrot]` cwd routing           |

## Reverting

```bash
systemctl --user disable --now telegram-gateway
cd "/home/tianyizhang/opus trading"
.venv/bin/python -m concierge.service &        # or however it was started before
```

Both pollers cannot run at the same time — Telegram's `getUpdates` permits
only one consumer per bot (HTTP 409 otherwise).
```
