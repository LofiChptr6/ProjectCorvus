# Concierge — Sonnet-backed Telegram Chat-Ops

A long-running Python service that owns the Telegram `getUpdates` loop and
routes inbound user messages through Claude Sonnet with tool access to the
trading desk. Gives you a 24/7 chat interface — from your phone — to query
positions, P&L, proposals, and raise/approve changes without waiting for a
scheduled Claude Code run.

## What it can do

- Answer natural-language questions: *"what's Rex's P&L today?"*, *"is atlas
  holding anything overnight?"*, *"quote SPY"*
- Resolve pending proposals by ID or "the oldest one" via conversational
  language (plus the existing `y`/`n` fast path)
- Raise new `propose_strategic_change` proposals on your behalf — e.g. *"pause
  titan for tomorrow, VIX is too low"* — and ping you to confirm
- Re-nudge stale proposals every 60 s while it's running (faster than the
  5-minute default when the scheduled path is in use)

## What it CANNOT do

By design, the following are **not exposed to Sonnet**:

- `place_order` / `cancel_order` / `modify_order`
- `activate_kill_switch`
- Direct allocation changes

If you ask the concierge to *"pause atlas"*, it files a proposal — it does not
execute the change. That keeps the existing approval gate intact.

## Starting / stopping

Two recommended setups; pick the one that matches how reliable you need it.

### Foreground (easiest, for testing)

**Linux/macOS:**
```bash
scripts/start_concierge.sh         # streams logs to stdout
# Ctrl-C to stop. Graceful shutdown pings Telegram.
```

**Windows:** double-click `scripts\start_concierge.bat`. Ctrl-C to stop.

Closing the terminal/window kills the concierge — fine for ad-hoc testing,
not for overnight reliability.

### Always-on (recommended)

**Linux/macOS — systemd user service.** The recommended production setup.
See [INSTALL.md §6](../INSTALL.md) for the full procedure. Summary:

```bash
chmod +x scripts/start_concierge.sh scripts/stop_concierge.sh
mkdir -p ~/.config/systemd/user
sed "s|@REPO_PATH@|$(pwd)|g" scripts/concierge.service \
    > ~/.config/systemd/user/trading-concierge.service
systemctl --user daemon-reload
systemctl --user enable --now trading-concierge
loginctl enable-linger "$USER"      # survive logout
```

Manage with `systemctl --user {status,restart,stop} trading-concierge` and
tail logs via `journalctl --user -u trading-concierge -f`.

**Windows — NSSM service.** Run `scripts\install_concierge_service.bat` as
administrator (requires `nssm.exe` on PATH). Creates a `TradingConcierge`
service that auto-starts on boot. `net start/stop TradingConcierge` to
manage; `scripts\uninstall_concierge_service.bat` to remove. Less common
now that we've migrated; kept for reference.

**Lock invariant:** regardless of mode, only one concierge runs at a time.
`data/concierge.lock` holds the PID; a second start aborts with a clear
error. Stale locks (dead PIDs) are auto-cleaned on next start.

## Env vars

Required in `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=...
```

Optional:

```
CONCIERGE_MODEL=claude-sonnet-4-5-20250929   # override the config.yaml default
CONCIERGE_DAILY_USD_CAP=5.00                 # override the config.yaml cap
TELEGRAM_CHAT_ID=...                         # pin the allowed chat (otherwise auto-detected)
```

## Config (`config.yaml` → `concierge:`)

```yaml
concierge:
  enabled: true
  model: claude-sonnet-4-5-20250929
  max_tool_iterations: 5        # hard cap per user message
  history_turns: 40             # conversation history retained
  daily_usd_cap: 5.00           # UTC-midnight reset
  nudge_interval_s: 60
  allowed_tools: [...]          # whitelist; see TOOL_SCHEMAS in concierge/tools.py
```

## Slash commands (no LLM cost)

- `/status` — positions + P&L + open orders + pending proposals
- `/positions` — current open positions
- `/pnl` — today's per-agent P&L
- `/proposals` — list pending with short IDs
- `/pause <agent>` — raise a "pause this agent" proposal
- `/budget` — today's Sonnet spend vs. cap
- `/help` — list commands

## Safety features

1. **Single-poller lock** (`data/concierge.lock`) prevents double-start. The
   scheduled commands detect the lock and delegate Telegram handling to the
   concierge — no offset races.
2. **Per-chat ACL** — messages from chat IDs other than `TELEGRAM_CHAT_ID`
   (or the cached chat) are dropped + logged.
3. **Write-action confirmation gate.** Any write tool (`resolve_proposal`,
   `propose_strategic_change`) stages an intent and asks you to reply `YES`
   before execution. Anything else cancels.
4. **Daily spend cap** enforced before each Sonnet call.
5. **Prompt-injection resistance** — "ignore previous instructions" in user
   text is treated as plain text by the system prompt.
6. **No direct trading** — see "What it cannot do" above.

## Coexistence with the scheduled commands

- `/mike-morning`, `/mike-midday`, `/cassidy-evening`, `/hourly-review` still
  call `process_telegram_inbox` as before.
- When the concierge is running, that tool returns `{concierge_online: true,
  delegated: true}` and does nothing else.
- When the concierge is **not** running, the old direct-poll path still works
  — scheduled commands keep functioning. Free-text chat won't be answered
  until you restart the concierge.

## Logs & state

| File | Purpose |
|------|---------|
| `logs/concierge.log` | rotating (5 MB × 3) log |
| `data/concierge.lock` | PID file — single-poller lock |
| `data/concierge_chat.json` | rolling conversation history |
| `data/concierge_usage.json` | today's Sonnet spend + token counts |
| `data/concierge_pending_confirm.json` | staged write-action intent, deleted after YES/cancel |
| `data/telegram_update_offset.txt` | Telegram offset — shared with fallback path |

## Troubleshooting

- **"Concierge cannot start: ANTHROPIC_API_KEY missing"** — set it in `.env`
  and restart.
- **"Daily budget reached"** — raise `CONCIERGE_DAILY_USD_CAP` or wait for
  UTC-midnight reset.
- **Replies stop coming** — check `logs/concierge.log` for crashes (or
  `journalctl --user -u trading-concierge` on Linux). The service is
  conservative and will log + continue on most errors, but an Anthropic
  outage will surface as a plain-text error message back to you.
- **Key rotation** — rotate at console.anthropic.com, replace the value in
  `.env`, then `systemctl --user restart trading-concierge` (Linux) or
  Ctrl-C + restart `start_concierge.bat` (Windows). The key that was pasted
  in chat on 2026-04-24 **should** be rotated.
