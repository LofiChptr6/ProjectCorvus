#!/usr/bin/env bash
# One-shot installer for IB Gateway + IBC on this Fedora box.
#
# Idempotent: re-running re-downloads only if files are missing. Uses sudo
# for /opt writes, dnf, and systemd unit install. Run once after pulling
# this branch:
#
#   ./scripts/install_ibc.sh
#
# Then add IBKR_USER and IBKR_PASSWORD to .env and start the units:
#   sudo systemctl enable --now ibgateway.service ibkr-daemon.service

set -euo pipefail

PROJECT_ROOT="/home/tianyizhang/opus trading"
IBC_PATH="/opt/ibc"
GATEWAY_PATH="/opt/ibgateway"
JTS_PATH="${HOME}/Jts"
GW_INSTALLER_URL="https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh"
DOWNLOAD_DIR="${HOME}/.cache/ibc-install"

if [[ $EUID -eq 0 ]]; then
    echo "install_ibc: run as your normal user, not root. sudo is invoked internally." >&2
    exit 1
fi

mkdir -p "${DOWNLOAD_DIR}"

# ── 1. system packages ─────────────────────────────────────────────────────
echo "[1/5] installing system packages (Xvfb, Java, helpers)..."
sudo dnf install -y --skip-unavailable \
    xorg-x11-server-Xvfb \
    xdpyinfo \
    java-21-openjdk-headless \
    gettext \
    unzip \
    curl

# ── 2. download IB Gateway stable installer ────────────────────────────────
GW_INSTALLER="${DOWNLOAD_DIR}/ibgateway-stable-standalone-linux-x64.sh"
if [[ ! -s "${GW_INSTALLER}" ]]; then
    echo "[2/5] downloading IB Gateway stable..."
    curl -fL --retry 3 -o "${GW_INSTALLER}" "${GW_INSTALLER_URL}"
    chmod +x "${GW_INSTALLER}"
else
    echo "[2/5] IB Gateway installer already present, skipping download"
fi

# ── 3. install IB Gateway to /opt (console mode, unattended) ───────────────
# InstallAnywhere flags: -i silent + properties for install dir.
if [[ ! -d "${GATEWAY_PATH}" ]] || ! find "${GATEWAY_PATH}" -mindepth 1 -maxdepth 1 -type d -regex '.*/[0-9]+$' -printf '.' | grep -q .; then
    echo "[3/5] installing IB Gateway to ${GATEWAY_PATH}..."
    sudo mkdir -p "${GATEWAY_PATH}"
    # Silent install. The installer reads app.install4j/response.varfile if
    # present; otherwise InstallAnywhere uses defaults. Newer IBKR installers
    # use install4j: pass -q -dir=... for non-interactive install.
    # Install to a temp dir name; we rename to the IBC version convention below
    GW_TMP_DIR="${GATEWAY_PATH}/_pending"
    sudo "${GW_INSTALLER}" -q -dir "${GW_TMP_DIR}" || {
        echo "install_ibc: silent install failed. Run manually:" >&2
        echo "  sudo ${GW_INSTALLER} -c" >&2
        echo "and point it at ${GATEWAY_PATH}/<version>" >&2
        exit 5
    }
    # Detect Gateway's reported major.minor from "IB Gateway X.Y.desktop"
    # and rename the dir to IBC's convention (concat with no dot: 10.45 → 1045).
    GW_DOTTED="$(find "${GW_TMP_DIR}" -maxdepth 1 -name 'IB Gateway *.desktop' \
        -printf '%f\n' | sed -E 's/^IB Gateway ([0-9]+\.[0-9]+)\.desktop$/\1/' | head -n 1)"
    if [[ -z "${GW_DOTTED}" ]]; then
        echo "install_ibc: could not detect Gateway version from install dir" >&2
        exit 7
    fi
    GW_VER="${GW_DOTTED//./}"   # e.g. 10.45 → 1045
    sudo mv "${GW_TMP_DIR}" "${GATEWAY_PATH}/${GW_VER}"
    echo "       installed IB Gateway ${GW_DOTTED} at ${GATEWAY_PATH}/${GW_VER}"
else
    echo "[3/5] IB Gateway already installed at ${GATEWAY_PATH}, skipping"
fi

# ── 4. download + install IBC ──────────────────────────────────────────────
if [[ ! -x "${IBC_PATH}/scripts/ibcstart.sh" ]]; then
    echo "[4/5] downloading latest IBC release..."
    IBC_ZIP_URL="$(curl -fsSL https://api.github.com/repos/IbcAlpha/IBC/releases/latest \
        | grep -oE 'https://[^"]+IBCLinux[^"]+\.zip' | head -n 1)"
    if [[ -z "${IBC_ZIP_URL}" ]]; then
        echo "install_ibc: could not resolve latest IBC release URL from GitHub" >&2
        exit 6
    fi
    IBC_ZIP="${DOWNLOAD_DIR}/$(basename "${IBC_ZIP_URL}")"
    curl -fL --retry 3 -o "${IBC_ZIP}" "${IBC_ZIP_URL}"
    sudo mkdir -p "${IBC_PATH}"
    sudo unzip -oq "${IBC_ZIP}" -d "${IBC_PATH}"
    sudo chmod +x "${IBC_PATH}"/scripts/*.sh "${IBC_PATH}"/*.sh 2>/dev/null || true
else
    echo "[4/5] IBC already installed at ${IBC_PATH}, skipping"
fi

# ── 5. install systemd units ───────────────────────────────────────────────
echo "[5/5] installing systemd units..."
sudo install -m 644 "${PROJECT_ROOT}/scripts/systemd/ibgateway.service"   /etc/systemd/system/ibgateway.service
sudo install -m 644 "${PROJECT_ROOT}/scripts/systemd/ibkr-daemon.service" /etc/systemd/system/ibkr-daemon.service
sudo systemctl daemon-reload

# Seed JTS settings dir
mkdir -p "${JTS_PATH}"

echo
echo "── install complete ───────────────────────────────────────────────────"
echo "Next steps (on this box, with .env loaded):"
echo "  1) Add to ${PROJECT_ROOT}/.env:"
echo "       IBKR_USER=<your gateway username>"
echo "       IBKR_PASSWORD=<your gateway password>"
echo "  2) Foreground sanity check in tmux:"
echo "       ${PROJECT_ROOT}/scripts/ibc-launch.sh"
echo "     → tap the IBKR Mobile push on your phone."
echo "  3) Enable systemd units:"
echo "       sudo systemctl enable --now ibgateway.service ibkr-daemon.service"
echo "  4) Watch:"
echo "       journalctl -u ibgateway.service -f"
echo "       journalctl -u ibkr-daemon.service -f"
