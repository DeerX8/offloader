# Footage Offloader v2 — Raspberry Pi 4

Dedicated appliance to transfer footage from USB-C SSDs to NAS via SMB.
Controlled via responsive web UI. Transfers run server-side (browser can be closed).

## Features

- **Background transfers** — Close your browser, the Pi keeps copying
- **Discord notifications** — Alerts at 25%, 50%, 75%, 100% with ETA
- **Reconnect anytime** — Open the UI from any device to see live progress
- **GitHub deploy** — `git pull` + restart for easy updates
- **mDNS** — Always accessible at `offloader.local:8080`
- **Tailscale** — Works remotely without port forwarding

## Quick Start (Local Install)

```bash
scp -r offloader-v2/ pi@raspberrypi.local:~/offloader
ssh pi@raspberrypi.local
cd ~/offloader
sudo bash install.sh
sudo tailscale up  # one-time auth
```

## GitHub-Based Install (Recommended)

```bash
# 1. Push this code to your GitHub repo
git init && git add -A && git commit -m "initial"
git remote add origin https://github.com/YOUR_USER/offloader.git
git push -u origin main

# 2. On the Pi, install from GitHub
ssh pi@raspberrypi.local
curl -fsSL https://raw.githubusercontent.com/YOUR_USER/offloader/main/install.sh | sudo bash -s -- --github https://github.com/YOUR_USER/offloader.git

# 3. Future updates (from any SSH session)
sudo bash /opt/offloader/update.sh
```

## Discord Setup

1. In your Discord server: Server Settings → Integrations → Webhooks → New Webhook
2. Copy the webhook URL
3. Open `http://offloader.local:8080` → ⚙ Settings → paste the webhook URL → Save

You'll get messages like:
```
🚀 Transfer started — 11 files, 111.1 GB → //100.109.23.38/archive/fishing
📊 50% complete — 55.5 GB / 111.1 GB — ETA: 8 min remaining
✅ Transfer complete — 111.1 GB, 11 files, 0 errors — 16m 42s
```

## Usage

1. Plug USB SSD into Pi's **blue USB 3.0 port**
2. Open `http://offloader.local:8080` on phone/laptop
3. Set project folder name (e.g., `fishing`)
4. Connect NAS → Select files → Transfer
5. **Close the browser** — transfer continues on the Pi
6. Get Discord alerts, or reopen the UI anytime to check progress

## Commands

```bash
sudo systemctl status offloader     # Status
sudo journalctl -u offloader -f     # Live logs
sudo systemctl restart offloader    # Restart
sudo bash /opt/offloader/update.sh  # Update from GitHub
sudo nano /etc/offloader/config.json # Edit config
```

## File Structure

```
/opt/offloader/          # Application code (git repo)
/etc/offloader/          # Config (preserved across updates)
/mnt/offloader/usb/      # USB mount (read-only)
/mnt/offloader/nas/      # NAS mount
```

## REST API

```bash
# Check status from scripts/shortcuts
curl http://offloader.local:8080/api/status | python3 -m json.tool
```
