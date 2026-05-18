#!/usr/bin/env bash
# Launch IB Gateway under IBC, headless.
#
# Reads IBKR_USER / IBKR_PASSWORD from the project .env, materializes a
# temp IBC config (mode 600, shredded on exit), starts a private Xvfb
# display, then execs IBC in the foreground so systemd owns the process tree.
#
# Designed to be the ExecStart of scripts/systemd/ibgateway.service. Can also
# be run by hand in a tmux session for the foreground sanity check.

set -euo pipefail

PROJECT_ROOT="/home/tianyizhang/opus trading"
IBC_PATH="/opt/ibc"
GATEWAY_PATH="/opt/ibgateway"
JTS_PATH="${HOME}/Jts"
DISPLAY_NUM=":99"

# ── load credentials from .env ─────────────────────────────────────────────
if [[ ! -f "${PROJECT_ROOT}/.env" ]]; then
    echo "ibc-launch: .env not found at ${PROJECT_ROOT}/.env" >&2
    exit 2
fi
set -a
# shellcheck source=/dev/null
source "${PROJECT_ROOT}/.env"
set +a

: "${IBKR_USER:?ibc-launch: IBKR_USER not set in .env}"
: "${IBKR_PASSWORD:?ibc-launch: IBKR_PASSWORD not set in .env}"

# ── detect installed Gateway major version ─────────────────────────────────
if [[ ! -d "${GATEWAY_PATH}" ]]; then
    echo "ibc-launch: IB Gateway not installed at ${GATEWAY_PATH}" >&2
    echo "ibc-launch: run scripts/install_ibc.sh first" >&2
    exit 3
fi
TWS_MAJOR_VRSN="$(find "${GATEWAY_PATH}" -mindepth 1 -maxdepth 1 -type d -regex '.*/[0-9]+$' -printf '%f\n' | sort -rn | head -n 1)"
if [[ -z "${TWS_MAJOR_VRSN}" ]]; then
    echo "ibc-launch: could not detect Gateway version under ${GATEWAY_PATH}" >&2
    exit 4
fi

# ── temp config (interpolate from template) ────────────────────────────────
TEMP_INI="$(mktemp /tmp/ibc-config.XXXXXX.ini)"
chmod 600 "${TEMP_INI}"

XVFB_PID=""
cleanup() {
    if [[ -n "${TEMP_INI:-}" && -f "${TEMP_INI}" ]]; then
        shred -u "${TEMP_INI}" 2>/dev/null || rm -f "${TEMP_INI}"
    fi
    if [[ -n "${XVFB_PID}" ]]; then
        kill "${XVFB_PID}" 2>/dev/null || true
    fi
    rm -f "/tmp/.X${DISPLAY_NUM#:}-lock" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

envsubst < "${PROJECT_ROOT}/scripts/ibc-config.ini.template" > "${TEMP_INI}"

# ── private X display for the Gateway Swing UI ─────────────────────────────
# Clear stale lock from a previous crash where the trap didn't fire
LOCK_FILE="/tmp/.X${DISPLAY_NUM#:}-lock"
if [[ -f "${LOCK_FILE}" ]]; then
    STALE_PID="$(tr -d ' ' < "${LOCK_FILE}" 2>/dev/null || true)"
    if [[ -n "${STALE_PID}" ]] && ! kill -0 "${STALE_PID}" 2>/dev/null; then
        rm -f "${LOCK_FILE}"
    fi
fi
Xvfb "${DISPLAY_NUM}" -screen 0 1024x768x16 -nolisten tcp &
XVFB_PID=$!
export DISPLAY="${DISPLAY_NUM}"

# Wait up to 10s for Xvfb to be ready
for _ in $(seq 1 20); do
    if xdpyinfo -display "${DISPLAY_NUM}" >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

mkdir -p "${JTS_PATH}"

# ── exec IBC in the foreground (systemd owns this pid) ─────────────────────
# IBC builds the Gateway path as ${tws-path}/ibgateway/${version}/, so pass
# the parent of GATEWAY_PATH (not GATEWAY_PATH itself).
exec "${IBC_PATH}/scripts/ibcstart.sh" "${TWS_MAJOR_VRSN}" \
    --gateway \
    --mode=live \
    --tws-path="$(dirname "${GATEWAY_PATH}")" \
    --tws-settings-path="${JTS_PATH}" \
    --ibc-path="${IBC_PATH}" \
    --ibc-ini="${TEMP_INI}"
