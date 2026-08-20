#!/usr/bin/env bash
# HostBot - one-shot VPS deployment (systemd, no Docker)
#
# Usage (run on your VPS from the repo clone):
#   sudo bash deploy.sh            # installs to /opt/hostbot
#   sudo bash deploy.sh /srv/hostbot   # custom destination
#
# Steps: installs system deps -> copies the bot -> creates the venv ->
# installs Python requirements -> installs + starts the systemd unit.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-/opt/hostbot}"

echo "==> Installing system packages (python3, venv, pip, nodejs, npm)..."
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip nodejs npm
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip nodejs npm
else
  echo "!! No apt-get/dnf found. Install python3, python3-venv, nodejs, npm manually."
fi

echo "==> Copying bot code to $DEST ..."
install -d "$DEST"
cp -r "$SCRIPT_DIR/bot" "$DEST/"

if [ -f "$SCRIPT_DIR/.env" ]; then
  cp "$SCRIPT_DIR/.env" "$DEST/bot/.env"
  echo "   .env copied."
else
  cp "$SCRIPT_DIR/.env.example" "$DEST/bot/.env"
  echo "!! No .env found - copied template. EDIT $DEST/bot/.env"
  echo "   then run: sudo systemctl restart hostbot"
fi

echo "==> Creating virtualenv and installing Python dependencies..."
python3 -m venv "$DEST/bot/.venv"
"$DEST/bot/.venv/bin/pip" install --quiet --upgrade pip
"$DEST/bot/.venv/bin/pip" install --quiet -r "$DEST/bot/requirements.txt"

echo "==> Installing systemd unit..."
SERVICE_TMP=$(mktemp)
sed "s|/opt/hostbot|$DEST|g" "$SCRIPT_DIR/hostbot.service" > "$SERVICE_TMP"
install -m 0644 "$SERVICE_TMP" /etc/systemd/system/hostbot.service
rm -f "$SERVICE_TMP"

systemctl daemon-reload
systemctl enable hostbot
systemctl restart hostbot

echo ""
echo "==> Done. Service status:"
systemctl status hostbot --no-pager || true
echo ""
echo "==> Follow logs with: journalctl -u hostbot -f"