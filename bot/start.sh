#!/usr/bin/env bash
# HostBot - start script for Linux VPS (systemd/Docker-free option)
# Creates the virtualenv on first run and installs dependencies.
set -euo pipefail

cd "$(dirname "$0")"

PYBIN=".venv/bin/python"

if [ ! -x "$PYBIN" ]; then
  echo "Creating virtual environment..."
  rm -rf .venv
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install --quiet --disable-pip-version-check -r requirements.txt

exec python hostbot.py