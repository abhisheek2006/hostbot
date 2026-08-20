#!/usr/bin/env bash
# HostBot - install Caddy reverse proxy (HTTPS) on the VPS
# Usage (on your VPS, from the repo clone):
#   sudo bash caddy/install.sh
# Then make sure AWS security group allows inbound TCP 80 and 443 (0.0.0.0/0).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing Caddy (official stable repo)..."
sudo apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl >/dev/null
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
sudo apt-get update -qq
sudo apt-get install -y -qq caddy

echo "==> Installing Caddyfile..."
sudo install -m 0644 "$SCRIPT_DIR/Caddyfile" /etc/caddy/Caddyfile

echo "==> Starting Caddy..."
sudo systemctl restart caddy
sudo systemctl enable caddy

echo ""
echo "==> Done. Test with:"
echo "   curl -m 10 https://13-60-251-8.sslip.io/health"
echo ""
echo "!! If the cert fails, check that AWS security group allows TCP 80 + 443 inbound."