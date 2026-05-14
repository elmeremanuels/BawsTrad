#!/usr/bin/env bash
# Run once on the Hetzner VPS as root (or with sudo) to set up the dashboard service.
# Dynamic paths — works regardless of repo name or location.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== BawsTrad dashboard setup ==="
echo "Repo root: $REPO_DIR"

# 0. Install Python package (makes 'from src.X import Y' work inside uv run)
# pyproject.toml has [build-system] + [tool.hatch.build.targets.wheel] packages=["src"]
# which installs src/ into the venv so all absolute imports resolve correctly.
echo "[0/4] Installing project package (uv sync)..."
cd "$REPO_DIR"
sudo -u trader /home/trader/.local/bin/uv sync --no-dev 2>/dev/null \
    || sudo -u trader /home/trader/.local/bin/uv sync
echo "Package installed."

# 1. Allow Tailscale traffic through UFW
echo "[1/5] Opening UFW for Tailscale interface..."
ufw allow in on tailscale0
ufw reload
echo "UFW updated."

# 2. Install systemd service
echo "[2/5] Installing systemd service..."
cp "$SCRIPT_DIR/trading-dashboard.service" /etc/systemd/system/trading-dashboard.service
systemctl daemon-reload
systemctl enable trading-dashboard
systemctl start trading-dashboard
echo "Service installed and started."

# 3. Sudo NOPASSWD for bot control from dashboard
# Uses /etc/sudoers.d/ — safer than appending directly to /etc/sudoers.
# visudo -cf validates syntax before activating (prevents locking yourself out).
echo "[3/5] Adding sudoers rule for systemctl restart..."
SUDOERS_FILE="/etc/sudoers.d/trading-bot-control"
cat > "$SUDOERS_FILE" <<'EOF'
trader ALL=(root) NOPASSWD: /bin/systemctl restart trading-bot
trader ALL=(root) NOPASSWD: /bin/systemctl stop trading-bot
trader ALL=(root) NOPASSWD: /bin/systemctl start trading-bot
EOF
chmod 0440 "$SUDOERS_FILE"
if ! visudo -cf "$SUDOERS_FILE"; then
    echo "ERROR: sudoers syntax invalid — removing file to prevent lockout"
    rm -f "$SUDOERS_FILE"
    exit 1
fi
echo "Sudoers rule installed at $SUDOERS_FILE."

# 4. Create state directory (used for pause flags and dashboard audit log)
echo "[4/5] Creating state directory..."
mkdir -p "$REPO_DIR/state"
chown trader:trader "$REPO_DIR/state"
echo "State dir ready at $REPO_DIR/state."

# 5. Verify import resolution works
echo "[5/5] Verifying Python imports..."
if sudo -u trader /home/trader/.local/bin/uv run python -c "from src.dashboard.data import get_today_stats; print('imports OK')" 2>/dev/null; then
    echo "Import check passed."
else
    echo "WARNING: import check failed — check uv sync output above."
fi

echo ""
echo "=== Done ==="
echo "Dashboard: http://$(tailscale ip -4 2>/dev/null || echo 'baws-bot'):8501"
echo "Status:    systemctl status trading-dashboard"
