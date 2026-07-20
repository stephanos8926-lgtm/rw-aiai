#!/bin/bash
# vdrive-host-setup — One-time setup of the vdrive on the host (srv1)
# 
# Run this ONCE as root on the host machine.
# Creates the vdrive image, installs the service, enables timers.
#
# Usage: sudo ./vdrive-host-setup.sh [/vdrive/backup.img] [20G]

set -euo pipefail

IMAGE_PATH="${1:-/vdrive/backup.img}"
SIZE="${2:-20G}"
CONFIG_DIR="/etc/vdrive"
SYSTEMD_DIR="/etc/systemd/system"

echo "========================================"
echo " vdrive-host-setup — srv1"
echo "========================================"
echo ""
echo "Image: $IMAGE_PATH"
echo "Size:  $SIZE"

# ─── Prerequisites ───────────────────────────────────────────────────────────

if [ "$EUID" -ne 0 ]; then
    echo "❌ This must be run as root (sudo)."
    exit 1
fi

# ─── Install scripts ─────────────────────────────────────────────────────────

echo ""
echo "📦 Installing vdrive-manager script..."
cp "$(dirname "$0")/vdrive-manager.sh" /usr/local/bin/vdrive-manager
chmod 755 /usr/local/bin/vdrive-manager

# ─── Install systemd units ───────────────────────────────────────────────────

echo "📦 Installing systemd units..."
SERVICE_DIR="$(dirname "$0")/../etc/systemd"
for unit in vdrive-manager.service vdrive-maintenance.service vdrive-maintenance.timer vdrive-sync.service vdrive-sync.timer; do
    if [ -f "$SERVICE_DIR/$unit" ]; then
        cp "$SERVICE_DIR/$unit" "$SYSTEMD_DIR/$unit"
        chmod 644 "$SYSTEMD_DIR/$unit"
        echo "  ✅ $unit"
    fi
done

# ─── Config ──────────────────────────────────────────────────────────────────

echo "📦 Installing config..."
mkdir -p "$CONFIG_DIR"
CONFIG_SRC="$(dirname "$0")/../etc/vdrive-config.yml"
if [ -f "$CONFIG_SRC" ]; then
    cp "$CONFIG_SRC" "$CONFIG_DIR/config.yml"
    chmod 644 "$CONFIG_DIR/config.yml"
    echo "  ✅ /etc/vdrive/config.yml"
fi

# ─── Create /vdrive directory ────────────────────────────────────────────────

echo ""
echo "📁 Creating $(dirname "$IMAGE_PATH")..."
mkdir -p "$(dirname "$IMAGE_PATH")"

# ─── Enable services ─────────────────────────────────────────────────────────

echo ""
echo "🔧 Enabling systemd services..."
systemctl daemon-reload

systemctl enable vdrive-manager.service
echo "  ✅ vdrive-manager.service (mounts @boot)"

systemctl enable vdrive-maintenance.timer
systemctl start vdrive-maintenance.timer
echo "  ✅ vdrive-maintenance.timer (daily grow+trim)"

# Remote sync is disabled by default — enable via config toggle
systemctl disable vdrive-sync.timer 2>/dev/null || true
echo "  ℹ️  vdrive-sync.timer (disabled — enable in config when ready)"

echo ""
echo "========================================"
echo " Host setup complete!"
echo "========================================"
echo ""
echo "Next steps:"
echo "  1. Run:  sudo vdrive-manager mount"
echo "     (creates $IMAGE_PATH, formats btrfs, mounts at /mnt/vdrive)"
echo ""
echo "  2. Verify:  sudo vdrive-manager status"
echo ""
echo "  3. To enable remote backup:"
echo "     Edit /etc/vdrive/config.yml → set remote_backup.enabled: true"
echo "     Then:  systemctl enable --now vdrive-sync.timer"
echo ""
