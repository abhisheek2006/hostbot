#!/usr/bin/env bash
# HostBot - start script for Linux VPS (systemd/Docker-free option)
set -e

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install --quiet -r requirements.txt

exec python hostbot.py