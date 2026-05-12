#!/usr/bin/env bash
# ProjectCorvus — one-command bootstrap for a fresh Linux install.
#
# What it does (idempotent — re-run is safe):
#   1. Detect distro, install system deps (python3.12+, postgres, cron)
#   2. Create main .venv (if missing) + pip install -r requirements.txt
#   3. Generate config.yaml from config.example.yaml (random pg password)
#   4. Generate .env from .env.example (prompts for API keys if interactive)
#   5. Create postgres `trading` role + database (via setup_trading_role.sql)
#   6. Initialize DB schema (db.schema.init_db())
#   7. Render + install systemd user timers (scripts/install_schedules.sh)
#   8. Run scripts/preflight.py to verify
#
# What it does NOT do (manual):
#   - Install IBKR Gateway / TWS (login + API permissions are gated by IBKR)
#   - Install the Claude Code CLI (`claude`) — install per
#     https://docs.anthropic.com/en/docs/claude-code/quickstart
#   - On local-llm branch: bootstrap .venv-vllm (needs python3.12 + ≥40 GB GPU)
#
# Use:    bash scripts/bootstrap.sh
# Re-run: harmless — each step is idempotent.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---------- pretty ----------------------------------------------------------

if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
    BOLD='\033[1m'; GREEN='\033[32m'; YELLOW='\033[33m'; RED='\033[31m'; DIM='\033[2m'; RESET='\033[0m'
else
    BOLD=''; GREEN=''; YELLOW=''; RED=''; DIM=''; RESET=''
fi

say() { printf "${BOLD}==>${RESET} %s\n" "$*"; }
note() { printf "${DIM}    %s${RESET}\n" "$*"; }
warn() { printf "${YELLOW}WARN${RESET}: %s\n" "$*"; }
fail() { printf "${RED}FAIL${RESET}: %s\n" "$*"; exit 1; }

# ---------- distro detection ------------------------------------------------

DISTRO_ID=""; DISTRO_LIKE=""
if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO_ID="${ID:-}"
    DISTRO_LIKE="${ID_LIKE:-}"
fi

pkg_install() {
    case "$DISTRO_ID $DISTRO_LIKE" in
        *fedora*|*rhel*|*centos*)
            sudo dnf install -y "$@"
            ;;
        *debian*|*ubuntu*)
            sudo apt-get update
            sudo apt-get install -y "$@"
            ;;
        *)
            warn "unknown distro ($DISTRO_ID) — install these manually: $*"
            return 1
            ;;
    esac
}

# ---------- step 1: system deps ---------------------------------------------

say "Step 1/8 — System packages"
case "$DISTRO_ID $DISTRO_LIKE" in
    *fedora*|*rhel*|*centos*)
        pkg_install python3 python3-pip postgresql postgresql-server postgresql-contrib cronie git
        # Init the Postgres data dir on RHEL-family if not done.
        if [[ ! -d /var/lib/pgsql/data/base ]]; then
            sudo postgresql-setup --initdb || true
        fi
        sudo systemctl enable --now postgresql || true
        sudo systemctl enable --now crond || true
        ;;
    *debian*|*ubuntu*)
        pkg_install python3 python3-venv python3-pip postgresql postgresql-contrib cron git
        sudo systemctl enable --now postgresql || true
        sudo systemctl enable --now cron || true
        ;;
    *)
        warn "Unknown distro — install python3 (3.12+), postgresql, cron, git manually, then re-run."
        ;;
esac

# Verify python is 3.12+
PY_BIN="$(command -v python3 || true)"
[[ -z "$PY_BIN" ]] && fail "python3 not found after install"
PY_VER="$($PY_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
case "$PY_VER" in
    3.12|3.13|3.14|3.15|3.16) note "python: $PY_BIN ($PY_VER)" ;;
    *) warn "python $PY_VER — repo targets 3.12+. Pinning a newer python is recommended." ;;
esac

# ---------- step 2: venv ----------------------------------------------------

say "Step 2/8 — Main venv (.venv)"
if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    note ".venv already present — refreshing requirements"
else
    "$PY_BIN" -m venv "$REPO_ROOT/.venv"
    note "created $REPO_ROOT/.venv"
fi
"$REPO_ROOT/.venv/bin/pip" install --quiet --upgrade pip
"$REPO_ROOT/.venv/bin/pip" install --quiet -r "$REPO_ROOT/requirements.txt"
note "$(${REPO_ROOT}/.venv/bin/pip list 2>/dev/null | wc -l) packages installed"

VENV_PY="$REPO_ROOT/.venv/bin/python"

# ---------- step 3: config.yaml ---------------------------------------------

say "Step 3/8 — config.yaml"
if [[ -f "$REPO_ROOT/config.yaml" ]]; then
    note "config.yaml exists — leaving as-is"
    PG_PASSWORD_VAL="$($VENV_PY -c "import yaml; print((yaml.safe_load(open('$REPO_ROOT/config.yaml')).get('postgres') or {}).get('password',''))")"
else
    PG_PASSWORD_VAL="$($VENV_PY -c "import secrets; print(secrets.token_urlsafe(24))")"
    note "generating new config.yaml with a random postgres password"
    sed "s|password: \"CHANGE_ME\"|password: \"$PG_PASSWORD_VAL\"|" \
        "$REPO_ROOT/config.example.yaml" > "$REPO_ROOT/config.yaml"
    chmod 0600 "$REPO_ROOT/config.yaml"
    note "wrote $REPO_ROOT/config.yaml (mode 0600)"
fi

# ---------- step 4: .env ----------------------------------------------------

say "Step 4/8 — .env"
if [[ -f "$REPO_ROOT/.env" ]]; then
    note ".env exists — leaving as-is"
else
    cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
    chmod 0600 "$REPO_ROOT/.env"
    warn "Wrote .env with placeholder values. Edit it now:"
    note "  TELEGRAM_BOT_TOKEN  (BotFather)"
    note "  MASSIVE_API_KEY     (https://massive.com/dashboard)"
    note "  ANTHROPIC_API_KEY   (only on the 'main' branch)"
fi

# ---------- step 5: postgres role + db --------------------------------------

say "Step 5/8 — Postgres role + database"
ROLE_EXISTS="$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='trading'" 2>/dev/null || echo "")"
DB_EXISTS="$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='trading'" 2>/dev/null || echo "")"

if [[ "$ROLE_EXISTS" == "1" ]] && [[ "$DB_EXISTS" == "1" ]]; then
    note "role 'trading' and database 'trading' already exist"
    # Reset password to whatever config.yaml has (idempotent).
    if [[ -n "$PG_PASSWORD_VAL" ]] && [[ "$PG_PASSWORD_VAL" != "CHANGE_ME" ]]; then
        sudo -u postgres psql -c "ALTER ROLE trading WITH LOGIN PASSWORD '$PG_PASSWORD_VAL'" >/dev/null
        note "reset trading role password to match config.yaml"
    fi
else
    [[ -z "$PG_PASSWORD_VAL" || "$PG_PASSWORD_VAL" == "CHANGE_ME" ]] && \
        fail "no usable postgres password in config.yaml — re-run after fixing"
    TMP_SQL="$(mktemp)"
    trap 'rm -f "$TMP_SQL"' EXIT
    sed "s|CHANGE_ME|$PG_PASSWORD_VAL|g" "$REPO_ROOT/scripts/setup_trading_role.sql" > "$TMP_SQL"
    sudo -u postgres psql -f "$TMP_SQL"
    note "created role + database 'trading'"
fi

# ---------- step 6: schema --------------------------------------------------

say "Step 6/8 — Database schema"
"$VENV_PY" - <<'PYEOF'
import asyncio
from db.schema import init_db, close_pool

async def main() -> None:
    await init_db()
    await close_pool()

asyncio.run(main())
print("schema ready")
PYEOF

# ---------- step 7: systemd units -------------------------------------------

say "Step 7/8 — systemd user units"
if ! command -v claude >/dev/null 2>&1; then
    warn "'claude' CLI not on PATH — skipping systemd install"
    note "Install Claude Code first, then re-run: scripts/install_schedules.sh"
else
    bash "$REPO_ROOT/scripts/install_schedules.sh"
fi

# ---------- step 8: preflight -----------------------------------------------

say "Step 8/8 — Preflight"
set +e
"$VENV_PY" "$REPO_ROOT/scripts/preflight.py"
PREFLIGHT_RC=$?
set -e

echo
if [[ $PREFLIGHT_RC -eq 0 ]]; then
    printf "${GREEN}Bootstrap complete.${RESET} Next:\n"
    note "1. Edit .env with real TELEGRAM_BOT_TOKEN, MASSIVE_API_KEY, etc."
    note "2. Launch IBKR Gateway, enable API (Edit → Global Configuration → API)."
    note "3. Smoke test one skill: bash scripts/run_scheduled_skill.sh atlas-review --dev"
    note "4. Tail logs: journalctl --user -u trading-mike-morning.service -f"
else
    printf "${YELLOW}Bootstrap finished with preflight failures — see above.${RESET}\n"
    note "Re-run preflight after fixing: .venv/bin/python scripts/preflight.py"
    exit $PREFLIGHT_RC
fi
