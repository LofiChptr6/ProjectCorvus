# Trading Desk — Linux Install Guide

End-to-end setup for a fresh Linux machine (Ubuntu/Debian assumed; adjust
package manager for RHEL/Arch). Goal: cron-driven sector agents + Mike
allocator + concierge running as a systemd service, all in **America/Phoenix**
local time (the desk's reference TZ).

If you ever need to redo this, the canonical scripts are everything under
`scripts/*.sh`. The `.bat` / `.ps1` files in that folder are leftover Windows
artefacts kept for reference; ignore them on Linux.

---

## 0. System prerequisites

```bash
# Timezone — every cron entry assumes America/Phoenix local.
sudo timedatectl set-timezone America/Phoenix
timedatectl    # verify

# Python 3.11+ + venv + build tools
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git cron postgresql postgresql-contrib

# Node (only if you plan to use the Anthropic CLI from npm; skip if installing claude via curl)
# curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
# sudo apt-get install -y nodejs
```

Install the Claude Code CLI per the official instructions at
<https://docs.anthropic.com/en/docs/claude-code/quickstart>. Verify:

```bash
claude --version
```

---

## 1. Clone and Python environment

```bash
git clone <repo-url> ~/trading
cd ~/trading

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 2. Postgres

```bash
sudo -u postgres createuser --pwprompt trading
sudo -u postgres createdb -O trading trading
```

Edit `config.yaml` (or set `PG_*` env vars) so the desk can connect. Then
initialise the schema:

```bash
source .venv/bin/activate
python -c "import asyncio; from db.schema import init_db, close_pool; \
asyncio.run((async lambda: (await init_db(), await close_pool()))())"
```

(That one-liner is awkward — easier: `python scripts/migrate_sqlite_to_postgres.py --schema-only` if available, or just let the first `mike-morning` run create tables.)

---

## 3. `.env`

Create `~/trading/.env` (keep it out of git):

```
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...           # optional; pins concierge to one chat
PG_HOST=localhost
PG_PORT=5432
PG_DATABASE=trading
PG_USER=trading
PG_PASSWORD=...
```

The concierge systemd unit reads this via `EnvironmentFile=`. Cron jobs
inherit env from the user crontab — either `source ~/trading/.env` from
`~/.profile` or wrap the launcher to `set -a; source .env; set +a` (the
launcher already does `cd "$REPO_ROOT"` so a file-relative source is safe).

---

## 4. IBKR Gateway

Install IBKR Gateway / TWS, log in, and enable the API:

- **Edit → Global Configuration → API → Settings**
  - ✅ Enable ActiveX and Socket Clients
  - Socket port: 4001 (Gateway) or 7496 (TWS) — must match `config.yaml`
  - Trusted IPs: 127.0.0.1
  - ❌ Read-Only API (we need to place orders)

Each scheduled skill connects with its own `IBKR_CLIENT_ID` (11–36; map
in `scripts/run_scheduled_skill.sh`). Don't reuse those IDs from any other
process.

---

## 5. Install scheduled jobs (cron)

```bash
chmod +x scripts/run_scheduled_skill.sh scripts/install_scheduled_tasks.sh
scripts/install_scheduled_tasks.sh
crontab -l | grep CLAUDE_TRADING   # verify 26 entries
```

Schedule (all America/Phoenix local):

| Time | Days | Skill |
|---|---|---|
| 06:06 | Mon-Fri | mike-morning |
| 06:30 | Mon-Fri | 10× sector-review (atlas/fab/fabless/rex/maya/titan/vera/trump/iron/volt) |
| 06:30–13:30 hourly | Mon-Fri | mike-allocator (8 runs through market close) |
| 08:00 | Mon-Fri | mike-midday |
| 16:00 | Mon-Fri | 10× sector-evening |
| 23:00 | Mon-Fri | cassidy-evening |
| 23:00 | Saturday | sector-archivist (weekly memory pass) |
| every hour | Mon-Fri | hourly-review (heartbeat) |

The launcher writes per-skill logs to `logs/<skill>.log` and sets the
canonical `IBKR_CLIENT_ID` for each.

---

## 6. Concierge (systemd user service)

Optional but recommended — it gives you 24/7 Telegram chat ops.

```bash
chmod +x scripts/start_concierge.sh scripts/stop_concierge.sh

# Render the unit with the absolute repo path and install for your user.
mkdir -p ~/.config/systemd/user
sed "s|@REPO_PATH@|$(pwd)|g" scripts/concierge.service \
    > ~/.config/systemd/user/trading-concierge.service

systemctl --user daemon-reload
systemctl --user enable --now trading-concierge
loginctl enable-linger "$USER"        # survive logout

# Check
systemctl --user status trading-concierge
journalctl --user -u trading-concierge -f
```

To stop: `systemctl --user stop trading-concierge`. The lock at
`data/concierge.lock` is reaped automatically on graceful shutdown.

---

## 7. Smoke test

Run a single skill in dev mode (no orders, prefixes Telegram with `[DEV]`):

```bash
scripts/run_scheduled_skill.sh atlas-review --dev
tail -f logs/atlas-review.log
```

You should see a Telegram message land within a minute or two.

Then verify the consolidated view + allocator dry-run:

```bash
scripts/run_scheduled_skill.sh mike-allocator --dev
```

---

## 8. Operational notes

- **DST:** the box is on America/Phoenix (no DST), so cron times are stable
  year-round. The `mike-morning` skill's "DST guard" section is informational
  only on this setup.
- **Logs:** `logs/*.log` rotate manually — wire up `logrotate` if you want.
- **DB cleanup:** `sector-archivist` runs weekly (Sat 23:00) and condenses
  30+day-old agent rows into narrative chapters before pruning. See
  `memory/project_sector_archivist.md` (in the user's Claude Code memory).
- **Updating:** `git pull && pip install -r requirements.txt && systemctl --user restart trading-concierge` is the usual cycle. Cron picks up new launcher contents on next fire (no reload needed).
