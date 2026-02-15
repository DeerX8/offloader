#!/bin/bash
set -e
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
APP_DIR="/opt/offloader"
CONFIG_DIR="/etc/offloader"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GITHUB_REPO=""
while [[ $# -gt 0 ]]; do case $1 in --github) GITHUB_REPO="$2"; shift 2 ;; *) shift ;; esac; done

echo -e "${CYAN}╔══════════════════════════════════════════╗"
echo "║     Footage Offloader v2 Installer       ║"
echo -e "╚══════════════════════════════════════════╝${NC}"
[ "$EUID" -ne 0 ] && echo -e "${RED}Run as root: sudo bash install.sh${NC}" && exit 1

echo -e "\n${YELLOW}[1/7] System packages...${NC}"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip cifs-utils exfatprogs exfat-fuse ntfs-3g avahi-daemon avahi-utils rsync usbutils curl git > /dev/null 2>&1
echo -e "${GREEN}  ✓ Done${NC}"

echo -e "\n${YELLOW}[2/7] Tailscale...${NC}"
if command -v tailscale &>/dev/null; then echo -e "${GREEN}  ✓ Already installed${NC}"
else curl -fsSL https://tailscale.com/install.sh | sh; echo -e "${GREEN}  ✓ Installed${NC}"; fi
tailscale status &>/dev/null && echo -e "${GREEN}  ✓ Connected ($(tailscale ip -4 2>/dev/null))${NC}" || echo -e "${YELLOW}  ! Run: sudo tailscale up${NC}"

echo -e "\n${YELLOW}[3/7] Hostname → offloader.local${NC}"
[ "$(hostname)" != "offloader" ] && hostnamectl set-hostname offloader && sed -i "s/127.0.1.1.*/127.0.1.1\toffloader/" /etc/hosts
systemctl enable avahi-daemon --now >/dev/null 2>&1
echo -e "${GREEN}  ✓ mDNS enabled${NC}"

echo -e "\n${YELLOW}[4/7] Application files...${NC}"
mkdir -p "$CONFIG_DIR" /mnt/offloader/usb /mnt/offloader/nas
if [ -n "$GITHUB_REPO" ]; then
  if [ -d "$APP_DIR/.git" ]; then cd "$APP_DIR" && git pull --ff-only
  else rm -rf "$APP_DIR" && git clone "$GITHUB_REPO" "$APP_DIR"; fi
  echo -e "${GREEN}  ✓ Installed from GitHub${NC}"
else
  mkdir -p "$APP_DIR/templates"
  cp "$SCRIPT_DIR/app.py" "$APP_DIR/app.py"
  cp "$SCRIPT_DIR/templates/index.html" "$APP_DIR/templates/index.html"
  echo -e "${GREEN}  ✓ Files copied${NC}"
fi

echo -e "\n${YELLOW}[5/7] Python venv...${NC}"
[ ! -d "$APP_DIR/venv" ] && python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet flask flask-socketio
echo -e "${GREEN}  ✓ Ready${NC}"

echo -e "\n${YELLOW}[6/7] Config...${NC}"
if [ ! -f "$CONFIG_DIR/config.json" ]; then
  cat > "$CONFIG_DIR/config.json" << 'EOF'
{
  "nas_ip": "100.109.23.38",
  "nas_ip_local": "192.168.88.20",
  "share_name": "archive",
  "subfolder": "",
  "smb_username": "",
  "smb_password": "",
  "smb_version": "3.0",
  "verify_checksums": false,
  "use_tailscale": true,
  "discord_webhook": "",
  "discord_notify_milestones": [25, 50, 75, 100]
}
EOF
  echo -e "${GREEN}  ✓ Default config created${NC}"
else echo -e "${GREEN}  ✓ Existing config kept${NC}"; fi

echo -e "\n${YELLOW}[7/7] Systemd service...${NC}"
cat > /etc/systemd/system/offloader.service << 'SVC'
[Unit]
Description=Footage Offloader
After=network-online.target tailscaled.service
Wants=network-online.target
[Service]
Type=simple
ExecStart=/opt/offloader/venv/bin/python /opt/offloader/app.py
WorkingDirectory=/opt/offloader
Restart=always
RestartSec=5
User=root
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal
SyslogIdentifier=offloader
[Install]
WantedBy=multi-user.target
SVC
systemctl daemon-reload && systemctl enable offloader && systemctl restart offloader
echo -e "${GREEN}  ✓ Service running${NC}"

# Update script
cat > "$APP_DIR/update.sh" << 'UPD'
#!/bin/bash
set -e
echo "Pulling latest..."
cd /opt/offloader && git pull --ff-only
/opt/offloader/venv/bin/pip install --quiet flask flask-socketio
echo "Restarting..."
systemctl restart offloader
echo "✓ Updated!"
UPD
chmod +x "$APP_DIR/update.sh"

echo -e "\n${GREEN}╔══════════════════════════════════════════╗"
echo -e "║         Installation Complete!           ║"
echo -e "╚══════════════════════════════════════════╝${NC}"
echo -e "\n  ${CYAN}Web UI:${NC}  ${YELLOW}http://offloader.local:8080${NC}"
tailscale status &>/dev/null && echo -e "  ${CYAN}Remote:${NC}  ${YELLOW}http://$(tailscale ip -4 2>/dev/null):8080${NC}"
echo -e "\n  ${CYAN}Update:${NC}  sudo bash /opt/offloader/update.sh"
echo -e "  ${CYAN}Logs:${NC}    sudo journalctl -u offloader -f"
echo -e "  ${CYAN}Status:${NC}  sudo systemctl status offloader\n"
