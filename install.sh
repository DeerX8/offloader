#!/bin/bash
# Offloader installation script for Raspberry Pi

set -e

echo "=== Offloader Installation ==="
echo

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (use sudo)"
    exit 1
fi

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

echo "→ Installing Python dependencies..."
pip3 install -r requirements.txt --break-system-packages

echo "→ Creating config directory..."
mkdir -p /etc/offloader

echo "→ Installing systemd service..."
cp offloader.service /etc/systemd/system/
systemctl daemon-reload

echo "→ Enabling auto-start on boot..."
systemctl enable offloader.service

echo "→ Starting service..."
systemctl start offloader.service

echo
echo "✓ Installation complete!"
echo
echo "Service status:"
systemctl status offloader.service --no-pager -l
echo
echo "View logs: sudo journalctl -u offloader.service -f"
echo "Access web UI: http://offloader.local:8080"
