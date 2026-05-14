#!/usr/bin/env bash
# Run once on the Hetzner VPS as root (or with sudo) to set up the dashboard service.
set -euo pipefail

echo "=== BawsTrad dashboard setup ==="

# 1. Allow Tailscale traffic through UFW
echo "[1/4] Opening UFW for Tailscale interface..."
ufw allow in on tailscale0
ufw reload
echo "UFW updated."

# 2. Install systemd service
echo "[2/4] Installing systemd service..."
cp /home/trader/BawsTrad/trading-bot/deploy/trading-dashboard.service \
   /etc/systemd/system/trading-dashboard.service
systemd daemon-reload
systemctl enable trading-dashboard
systemctl start trading-dashboard
echo "Service installed and started."

# 3. Sudo NOPASSWD for bot control from dashboard
echo "[3/4] Adding sudoers rule for systemctl restart..."
SUDOERS_LINE="trader ALL=(root) NOPASSWD: /bin/systemctl restart trading-bot, /bin/systemctl stop trading-bot, /bin/systemctl start trading-bot"
if ! grep -qF "NOPASSWD: /bin/systemctl restart trading-bot" /etc/sudoers; then
    echo "$SUDOERS_LINE" >> /etc/sudoers
    echo "Sudoers updated."
else
    echo "Sudoers rule already present."
fi

# 4. Create state directory
echo "[4/4] Creating state directory..."
mkdir -p /home/trader/BawsTrad/trading-bot/state
chown trader:trader /home/trader/BawsTrad/trading-bot/state
echo "State dir ready."

echo ""
echo "=== Done ==="
echo "Dashboard accessible at http://$(tailscale ip -4 2>/dev/null || echo 'baws-bot'):8501"
echo "Check status: systemctl status trading-dashboard"
