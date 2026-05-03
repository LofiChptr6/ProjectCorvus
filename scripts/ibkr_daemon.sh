#!/usr/bin/env bash
# Launcher for ibkr-daemon.service. Wrapped in a shell script because the
# project path "opus trading" contains a space, which systemd's ExecStart
# parser doesn't quote cleanly.
set -eu
cd "$(dirname "$(readlink -f "$0")")/.."
exec ./.venv/bin/python -m ibkr.daemon
