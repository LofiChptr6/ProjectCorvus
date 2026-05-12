# ProjectCorvus — Install Guide

End-to-end setup for a fresh Linux install (Fedora/RHEL or Debian/Ubuntu).
Everything below assumes the desk runs in **America/Phoenix** (no DST), which
is what every cron string and timer in the repo is keyed to.

## TL;DR

```bash
git clone <repo-url> "$HOME/trading"
cd "$HOME/trading"
bash scripts/bootstrap.sh          # idempotent — re-runnable
# edit .env (TELEGRAM_BOT_TOKEN, MASSIVE_API_KEY, …)
.venv/bin/python scripts/preflight.py
```

If preflight reports all-pass except IBKR Gateway, launch Gateway and re-run.

Read on for what the bootstrap does, branch differences, and the manual
pieces that can't be automated.

---

## 0. Pick the branch first

| Branch       | LLM backend                                          | Hardware                    | When to pick                                    |
|--------------|------------------------------------------------------|-----------------------------|-------------------------------------------------|
| **`main`**   | Anthropic API (`claude-opus-*`, `claude-sonnet-*`)   | Any Linux box               | **Default for new installs.** No GPU required.  |
| `local-llm`  | Local Qwen3-32B-FP8 via vLLM (`/v1/messages` shape)  | CUDA GPU **≥ 40 GB VRAM**   | You have a Blackwell/Ada/Hopper card and don't want to pay per-token. |

Both branches use the same desk logic, scheduling, and Postgres schema.
The only difference is where `claude` CLI requests get answered.

If you're not sure, stay on `main`.

---

## 1. Prerequisites that bootstrap can't install

The bootstrap script handles system packages, Postgres role/db, venv,
config templating, schema init, and systemd units. These pieces are
manual:

1. **Claude Code CLI** (`claude`).
   Install per <https://docs.anthropic.com/en/docs/claude-code/quickstart>.
   Verify with `claude --version`.

2. **IBKR Gateway or TWS.**
   Download from Interactive Brokers, log into a paper account, then:
   - Edit → Global Configuration → API → Settings
     - ✅ Enable ActiveX and Socket Clients
     - Socket port: `4002` (Gateway paper) / `4001` (Gateway live) / `7497` (TWS paper)
     - Trusted IPs: `127.0.0.1`
     - ❌ Read-Only API
   - The default `config.yaml` points at `127.0.0.1:4002`. Edit if needed.

3. **Timezone.** The desk's cron/timer schedule is keyed to
   America/Phoenix (no DST). Set the box's TZ:
   ```bash
   sudo timedatectl set-timezone America/Phoenix
   timedatectl
   ```
   You *can* run on another TZ but you'll need to shift every systemd
   timer's `OnCalendar=` line by hand.

4. **(local-llm branch only)** A Python 3.12 binary for the vLLM venv,
   and ~40 GB of free disk for model weights. See §6 below.

---

## 2. Run the bootstrap

```bash
git clone <repo-url> "$HOME/trading"
cd "$HOME/trading"
bash scripts/bootstrap.sh
```

`bootstrap.sh` is idempotent — re-running is safe. It will:

| Step | What | Notes |
|------|------|-------|
| 1    | Install `python3`, `postgresql`, `cron`, `git` | Auto-detects Fedora/RHEL vs Debian/Ubuntu |
| 2    | Create `.venv` + `pip install -r requirements.txt` | Skipped if `.venv/` exists |
| 3    | Generate `config.yaml` from the example | Inserts a random `pg_password` |
| 4    | Generate `.env` from `.env.example` | You **must** edit the placeholders before anything works |
| 5    | Create Postgres `trading` role + database | Runs `scripts/setup_trading_role.sql` as `postgres` user |
| 6    | Initialize the schema | Calls `db.schema.init_db()` |
| 7    | Render + install systemd user units | Calls `scripts/install_schedules.sh`; skipped if `claude` CLI missing |
| 8    | Run preflight | Reports what's still broken |

When it finishes, edit `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=<from @BotFather>
MASSIVE_API_KEY=<from https://massive.com/dashboard>
# Only on the 'main' branch:
ANTHROPIC_API_KEY=<from https://console.anthropic.com>
```

Then re-run preflight:

```bash
.venv/bin/python scripts/preflight.py
```

---

## 3. Preflight checks

`scripts/preflight.py` is the single source of truth for "is my install
ready?". It checks:

- Python is 3.12+
- `.venv` has the required packages
- `.env` and `config.yaml` exist with non-placeholder values
- `claude` CLI is on `PATH`
- Postgres is reachable and the schema is bootstrapped
- Telegram bot token validates via `getMe`
- Massive API key validates via a SPY snapshot
- IBKR Gateway is accepting connections on the configured port
- (local-llm branch) vLLM is responding on `LOCAL_LLM_BASE_URL`
- systemd user units are installed

Re-run as often as you want — read-only and fast.

---

## 4. systemd units — what installs

`scripts/install_schedules.sh` (called by bootstrap) renders the
templated units in `scripts/systemd/*.service|*.timer` with your repo
path, user, and `claude` binary location, then installs them as
**user-mode** units under `~/.config/systemd/user/`.

| Unit                              | Type            | Fires                                        |
|-----------------------------------|-----------------|----------------------------------------------|
| `trading-hourly-review.timer`     | weekday × hour  | Sector reviews + Mike allocator + heartbeat  |
| `trading-mike-morning.timer`      | weekday 06:06   | Deep market analysis (9:06 ET in EDT)        |
| `trading-mike-midday.timer`       | weekday 08:00   | Position reassessment (11:00 ET in EDT)      |
| `trading-cassidy-evening.timer`   | weekday 23:00   | End-of-day risk audit                        |
| `trading-sector-evenings.timer`   | weekday 20:00   | Per-sector attribution reviews               |
| `trading-news-ingest.timer`       | RTH every 15m   | Massive (Benzinga) → `news_items`            |
| `trading-refresh-agent-state.timer` | every hour :05 | Deterministic per-agent state snapshot       |
| `trading-weekly-tune.timer`       | Sun 13:00 UTC   | Weekly model-tune cycle                      |
| `trading-llm-proxy.service`       | long-running    | `obs/proxy.py` — captures `/v1/messages`     |
| `trading-dashboard.service`       | long-running    | `obs/dashboard.py` — Streamlit live monitor  |
| `trading-vllm*.service`           | long-running    | vLLM (local-llm branch only)                 |

Enable linger so user units survive logout:

```bash
sudo loginctl enable-linger "$USER"
```

Manage with:

```bash
systemctl --user list-timers --all
systemctl --user start trading-hourly-review.service     # manual fire
journalctl --user -u trading-mike-morning.service -f     # tail logs
bash scripts/uninstall_schedules.sh                      # remove all
```

---

## 5. Concierge (Telegram chat-ops)

The concierge is a long-running service that owns the Telegram poller.
`install_schedules.sh` doesn't install it (different lifecycle); use one
of:

```bash
# Mode C — per-user service, starts when you log in:
bash scripts/install_concierge_task.sh
loginctl enable-linger "$USER"        # survive logout

# Mode D — system service, starts at boot (needs sudo):
bash scripts/install_concierge_service.sh
```

See `concierge/README.md` for the full chat surface and safety model.

---

## 6. Local LLM bootstrap (`local-llm` branch only)

Skip on `main`. The `local-llm` branch replaces every Claude API call
with a locally served Qwen3-32B-FP8.

Requirements: CUDA GPU with **≥ 40 GB VRAM**, Python 3.12, ~40 GB free
disk for weights.

```bash
# 1. Separate venv (vLLM hates 3.14)
sudo dnf install python3.12-devel             # Fedora; or python3.12-dev on Debian
python3.12 -m venv .venv-vllm
.venv-vllm/bin/pip install --upgrade pip wheel
.venv-vllm/bin/pip install vllm openai

# 2. Pre-pull the model weights (~34 GB)
.venv-vllm/bin/hf download Qwen/Qwen3-32B-FP8 --max-workers 6

# 3. Re-run install_schedules.sh — it auto-detects .venv-vllm
#    and enables trading-vllm + trading-vllm-embed.
bash scripts/install_schedules.sh

# 4. Smoke test
curl -s http://127.0.0.1:8000/v1/models | jq
curl -s http://127.0.0.1:8000/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-opus-4-7","max_tokens":64,"messages":[{"role":"user","content":"Say OK"}]}' | jq
```

Rollback: `git checkout main && systemctl --user restart trading-*`.
The `.venv-vllm/` and weight cache survive the checkout.

---

## 7. Smoke test the desk

Run one sector skill in dev mode (no orders placed, `[DEV]` prefix on
Telegram):

```bash
bash scripts/run_scheduled_skill.sh atlas-review --dev
tail -f logs/atlas-review.log
```

You should see a Telegram message land within a minute or two. Then try
the allocator dry-run:

```bash
bash scripts/run_scheduled_skill.sh mike-allocator --dev
```

If both work, you're done. The systemd timers will pick up from here.

---

## 8. Operational notes

- **DST.** The box is on America/Phoenix (no DST), so cron times are
  stable year-round. Mike's NY-anchored skills (`mike-morning` at 9:06
  ET, `mike-midday` at 11:00 ET) need their `OnCalendar=` hour shifted
  by ±1 twice a year — see the comments inside
  `scripts/systemd/trading-mike-morning.timer`.
- **Logs.** `logs/*.log` rotate manually — wire up `logrotate` if you
  want persistent rotation. journald keeps service-level logs anyway.
- **DB cleanup.** `trading-weekly-tune.timer` runs the model-tune
  cycle; the sector-archivist skill (separately scheduled) condenses
  30+day-old rows into narrative chapters.
- **Updating.**
  ```bash
  git pull
  .venv/bin/pip install -r requirements.txt
  bash scripts/install_schedules.sh           # re-render units in case templates changed
  systemctl --user restart trading-concierge
  ```
- **IBKR client IDs.** Each scheduled skill connects with its own
  `IBKR_CLIENT_ID` (mapped in `scripts/run_scheduled_skill.sh`). Don't
  reuse those IDs from any other process — IBKR silently knocks the
  loser off the connection.
