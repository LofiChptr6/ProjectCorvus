# Concierge — Local-LLM-backed Telegram Chat-Ops

A long-running Python service that owns the Telegram `getUpdates` loop and
routes inbound user messages through the local LLM (vLLM-served, default
`Qwen/Qwen3-32B-FP8`) with tool access to the trading desk. Gives you a 24/7
chat interface — from your phone — to query positions, P&L, proposals, and
raise/approve changes without waiting for a scheduled run.

## What it can do

- Answer natural-language questions: *"what's Rex's P&L today?"*, *"is atlas
  holding anything overnight?"*, *"quote SPY"*
- Resolve pending proposals by ID or "the oldest one" via conversational
  language (plus the existing `y`/`n` fast path and inline-button taps)
- Bulk-action every pending proposal at once (*"drop all pending approvals"*)
- Raise new `propose_strategic_change` proposals on your behalf — e.g. *"pause
  energy for tomorrow, OPEC headline risk"* — and ping you to confirm
- Re-nudge stale proposals every 60 s while it's running

## What it CANNOT do

By design, the following are **not exposed to the concierge**:

- `place_order` / `cancel_order` / `modify_order`
- `activate_kill_switch`
- Direct allocation changes

If you ask the concierge to *"pause atlas"*, it files a proposal — it does not
execute the change. That keeps the existing approval gate intact.

## Three-stream messaging pool

Every Telegram event (inbound + outbound) is logged to the `telegram_message`
Postgres table with a `kind` discriminator that partitions the traffic:

| `kind`            | Visible to LLM context? | Role                                                                |
|-------------------|-------------------------|---------------------------------------------------------------------|
| `user_text`       | yes                     | inbound free-text question from the user                            |
| `concierge_reply` | yes                     | outbound LLM reply                                                  |
| `concierge_tool` | yes (replay only)       | role=tool rows from the tool-use loop (internal, never on Telegram) |
| `slash_cmd`       | no                      | inbound `/help`, `/status`, … and their canned outputs              |
| `approval`        | no                      | proposal pings, `/y`/`/n` replies, inline-button taps, confirmations |
| `push`            | no                      | agent-initiated notifications (reports, digests, alerts)            |

The LLM sees only the first three. Approval traffic and push notifications
flow around it. The concierge has dedicated tools (`list_pending_proposals`,
`list_recent_decisions`, `resolve_all_pending`, …) to inspect those streams on
demand instead.

## Starting / stopping

The canonical setup is a systemd user unit. To install:

```bash
cp scripts/systemd/trading-concierge.user.service \
   ~/.config/systemd/user/trading-concierge.service
systemctl --user daemon-reload
systemctl --user enable --now trading-concierge
loginctl enable-linger "$USER"      # survive logout
```

Manage with `systemctl --user {status,restart,stop} trading-concierge` and
tail logs via `journalctl --user -u trading-concierge -f` (or
`tail -f logs/concierge.log`).

**Lock invariant:** `data/concierge.lock` holds the running PID; a second
start aborts with a clear error. Stale locks (dead PIDs) are auto-cleaned on
next start.

## Env vars

Required in `.env`:

```
TELEGRAM_BOT_TOKEN=...
LOCAL_LLM_BASE_URL=http://localhost:8000/v1
LOCAL_MODEL=Qwen/Qwen3-32B-FP8
```

Optional:

```
CONCIERGE_MODEL=Qwen/Qwen3-32B-FP8           # override config.yaml's model
CONCIERGE_DAILY_TOKEN_CAP=2000000            # halts after this many in+out tokens (UTC reset)
LOCAL_LLM_API_KEY=local-dummy                # vLLM doesn't auth, but the SDK requires a string
TELEGRAM_CHAT_ID=...                         # pin the allowed chat (otherwise auto-detected)
```

## Config (`config.yaml` → `concierge:`)

```yaml
concierge:
  enabled: true
  model: Qwen/Qwen3-32B-FP8
  max_tool_iterations: 5        # hard cap per user message
  history_messages: 30          # rolling user/assistant rows shown to the LLM each turn
  daily_token_cap: 2000000      # halts when in+out exceed this (UTC reset)
  nudge_interval_s: 60
  allowed_tools: [...]          # whitelist; see TOOL_SCHEMAS in concierge/tools.py
```

## Slash commands (no LLM cost)

- `/status` — positions + P&L + open orders + pending proposals
- `/positions` — current open positions
- `/pnl` — today's per-agent P&L
- `/proposals` — list pending with short IDs
- `/pause <agent>` — raise a "pause this agent" proposal
- `/budget` — today's local-LLM token usage vs. cap
- `/help` — list commands

## Safety features

1. **Single-poller lock** (`data/concierge.lock`) prevents double-start.
2. **Per-chat ACL** — messages from chat IDs other than `TELEGRAM_CHAT_ID`
   (or the cached chat) are dropped + logged.
3. **Write-action confirmation gate.** Any write tool (`resolve_proposal`,
   `resolve_all_pending`, `propose_strategic_change`) stages an intent and
   asks you to reply `YES` before execution. Anything else cancels.
4. **Daily token cap** enforced before each LLM call.
5. **Prompt-injection resistance** — "ignore previous instructions" in user
   text is treated as plain text by the system prompt.
6. **No direct trading** — see "What it cannot do" above.

## Coexistence with the scheduled commands

The concierge is the **only** Telegram poller. Scheduled skills
(`/mike-morning`, `/mike-midday`, `/cassidy-evening`, `/hourly-review`,
sector reviews) never touch Telegram getUpdates — proposals raised by those
skills appear in the user's Telegram via `send_telegram_update` / the
proposal-ping path, and the user's `/y`/`/n` replies (or inline-button taps)
are handled by the concierge directly.

If the concierge service is down, `/y`/`/n` replies queue in Telegram's
24h getUpdates buffer; restart the service to drain them.

## Logs & state

| File / table | Purpose |
|------|---------|
| `logs/concierge.log` | rotating (5 MB × 3) log |
| `data/concierge.lock` | PID file — single-poller lock |
| `data/concierge_usage.json` | today's token counts + request total |
| `data/concierge_pending_confirm.json` | staged write-action intent, deleted after YES/cancel |
| `data/telegram_update_offset.txt` | Telegram getUpdates offset |
| `telegram_message` (Postgres) | every inbound/outbound Telegram event, kind-tagged |

## Troubleshooting

- **Replies stop coming** — check `logs/concierge.log` for crashes (or
  `journalctl --user -u trading-concierge`). Most common cause: another
  process is also calling Telegram `getUpdates` for the same bot, which
  returns HTTP 409 Conflict. Find and kill it (`ps auxf | grep telegram`).
- **"Concierge error talking to local model"** — vLLM is unreachable. Verify
  with `curl http://localhost:8000/v1/models`, then check
  `systemctl --user status trading-vllm`.
- **Daily token cap hit** — raise `CONCIERGE_DAILY_TOKEN_CAP` in `.env` or
  wait for UTC-midnight reset.
- **`<think>` blocks leak into Telegram** — should not happen; chat.py
  strips them. If they do, the LLM may be using a non-standard reasoning
  delimiter and `_THINK_RE` needs widening.
