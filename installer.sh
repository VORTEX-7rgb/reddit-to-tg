#!/usr/bin/env bash
# Karmabot installer — runs as root on Ubuntu 22.04+
# Sets up: system user, venv, dependencies, dirs, systemd units
# After running, edit /opt/karmabot/config.yaml with your credentials.

set -euo pipefail

INSTALL_DIR="/opt/karmabot"
DATA_DIR="/var/lib/karmabot"
LOG_DIR="/var/log/karmabot"
SERVICE_USER="karmabot"

echo "=== Karmabot Installer ==="
echo "Install dir:  $INSTALL_DIR"
echo "Data dir:     $DATA_DIR"
echo "Log dir:      $LOG_DIR"
echo "Service user: $SERVICE_USER"
echo ""

# Must be root
if [[ $EUID -ne 0 ]]; then
  echo "ERROR: Run as root: sudo bash installer.sh"
  exit 1
fi

# ─── 1. System dependencies ─────────────────────────────────
echo "[1/8] Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
  python3 python3-venv python3-pip \
  ffmpeg \
  ca-certificates curl \
  ufw fail2ban

# ─── 2. Create service user ─────────────────────────────────
echo "[2/8] Creating service user '$SERVICE_USER'..."
if ! id "$SERVICE_USER" &>/dev/null; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# ─── 3. Create directories ──────────────────────────────────
echo "[3/8] Creating directories..."
mkdir -p "$INSTALL_DIR" "$DATA_DIR/media" "$LOG_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR" "$LOG_DIR"

# ─── 4. Copy source code ────────────────────────────────────
echo "[4/8] Copying source code..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp -r "$SCRIPT_DIR/src" "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR/systemd" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/config.example.yaml" "$INSTALL_DIR/"

# If config.yaml doesn't exist yet, create from example
if [[ ! -f "$INSTALL_DIR/config.yaml" ]]; then
  cp "$INSTALL_DIR/config.example.yaml" "$INSTALL_DIR/config.yaml"
  echo "  -> Created config.yaml from example. EDIT IT before starting service."
fi
chmod 600 "$INSTALL_DIR/config.yaml"
chown -R "root:$SERVICE_USER" "$INSTALL_DIR"
chmod -R o-rwx "$INSTALL_DIR"

# ─── 5. Python virtualenv ───────────────────────────────────
echo "[5/8] Creating Python venv..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip --quiet
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/venv"

# ─── 6. Install systemd units ───────────────────────────────
echo "[6/8] Installing systemd units..."
cp "$INSTALL_DIR/systemd/karmabot.service" /etc/systemd/system/
cp "$INSTALL_DIR/systemd/karmabot.timer" /etc/systemd/system/
systemctl daemon-reload

# ─── 7. Firewall (basic hardening) ──────────────────────────
echo "[7/8] Configuring firewall (ufw)..."
if ! ufw status | grep -q "Status: active"; then
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow 22/tcp comment 'SSH'
  # Do NOT open 80/443 unless you add a web dashboard later
  ufw --force enable
else
  echo "  -> ufw already active, skipping"
fi

# ─── 8. First-run Telegram login ────────────────────────────
echo "[8/8] First-run Telegram login..."
echo ""
echo "============================================"
echo "  MANUAL STEP REQUIRED: Telegram Login"
echo "============================================"
echo "The bot needs to log into Telegram ONCE to create a session file."
echo "This requires interactive phone-code entry, so it can't be automated."
echo ""
echo "Run this as the service user:"
echo ""
echo "  sudo -u $SERVICE_USER $INSTALL_DIR/venv/bin/python -m src.scheduler --config $INSTALL_DIR/config.yaml"
echo ""
echo "It will prompt for the Telegram login code (sent to your phone)."
echo "After successful login, the session file is saved at:"
echo "  $(pwd)/${SERVICE_USER}_session.session"
echo ""
echo "Then start the timer:"
echo "  sudo systemctl enable --now karmabot.timer"
echo "  sudo systemctl status karmabot.timer"
echo "  sudo journalctl -u karmabot.service -f"
echo ""
echo "Done. Edit $INSTALL_DIR/config.yaml FIRST, then do the login step above."
