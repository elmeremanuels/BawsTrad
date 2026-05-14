#!/usr/bin/env bash
# Fresh-deploy script for the Hetzner VPS.
# Run once as root (or with sudo) from anywhere in the repo tree.
# Sets up both the trading bot and the Streamlit dashboard as systemd services.
#
# Usage:
#   sudo bash deploy/setup.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

echo "╔══════════════════════════════════════════╗"
echo "║        BawsTrad fresh deploy             ║"
echo "╚══════════════════════════════════════════╝"
echo "Repo root : $REPO_DIR"
echo "Script dir: $SCRIPT_DIR"
echo ""

# ── 0. Install Python package ────────────────────────────────────────────────
echo "[0/6] Installing project package (uv sync)..."
cd "$REPO_DIR"
# Try --no-dev first (production); fall back to full sync if pyproject has no
# dev extras declared yet.
sudo -u trader /home/trader/.local/bin/uv sync --no-dev 2>/dev/null \
    || sudo -u trader /home/trader/.local/bin/uv sync
echo "      Package installed."

# ── 1. Tailscale / UFW ──────────────────────────────────────────────────────
echo "[1/6] Opening UFW for Tailscale interface..."
ufw allow in on tailscale0
ufw reload
echo "      UFW updated."

# ── 2. Install systemd services ─────────────────────────────────────────────
echo "[2/6] Installing systemd services..."

cp "$SCRIPT_DIR/trading-bot.service"       /etc/systemd/system/trading-bot.service
cp "$SCRIPT_DIR/trading-dashboard.service" /etc/systemd/system/trading-dashboard.service

systemctl daemon-reload

# Bot service — enable but do NOT auto-start yet; operator should verify .env first
systemctl enable trading-bot
echo "      trading-bot.service installed (not started — verify .env first)."

# Dashboard service — start immediately
systemctl enable  trading-dashboard
systemctl restart trading-dashboard
echo "      trading-dashboard.service installed and started."

# ── 3. Sudoers for dashboard bot-control buttons ─────────────────────────────
echo "[3/6] Adding sudoers rule for systemctl restart/stop/start..."
SUDOERS_FILE="/etc/sudoers.d/trading-bot-control"
cat > "$SUDOERS_FILE" <<'EOF'
trader ALL=(root) NOPASSWD: /bin/systemctl restart trading-bot
trader ALL=(root) NOPASSWD: /bin/systemctl stop    trading-bot
trader ALL=(root) NOPASSWD: /bin/systemctl start   trading-bot
EOF
chmod 0440 "$SUDOERS_FILE"
if ! visudo -cf "$SUDOERS_FILE"; then
    echo "ERROR: sudoers syntax invalid — removing file to prevent lockout"
    rm -f "$SUDOERS_FILE"
    exit 1
fi
echo "      Sudoers rule installed at $SUDOERS_FILE."

# ── 4. State directory ───────────────────────────────────────────────────────
echo "[4/6] Creating state directory..."
mkdir -p "$REPO_DIR/state"
chown trader:trader "$REPO_DIR/state"
echo "      State dir ready at $REPO_DIR/state."

# ── 5. Smoke-test imports ────────────────────────────────────────────────────
echo "[5/6] Verifying Python imports..."
if sudo -u trader /home/trader/.local/bin/uv run python \
        -c "from src.dashboard.data import get_today_stats; print('      imports OK')" 2>/dev/null; then
    : # message already printed
else
    echo "      WARNING: import check failed — check uv sync output above."
fi

# ── 6. Summary ───────────────────────────────────────────────────────────────
TAILSCALE_IP="$(tailscale ip -4 2>/dev/null || echo 'baws-bot')"
echo ""
echo "[6/6] Done."
echo ""
echo "  Dashboard : http://${TAILSCALE_IP}:8501"
echo "  Bot status: systemctl status trading-bot"
echo "  Bot logs  : journalctl -u trading-bot -f"
echo ""
echo "  Next steps:"
echo "    1. Verify /home/trader/BawsTrad/.env has all API keys"
echo "    2. sudo systemctl start trading-bot"
echo "    3. sudo journalctl -u trading-bot -n 50 --no-pager"
