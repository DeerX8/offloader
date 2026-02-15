# Offloader Installation

## Quick Install (Raspberry Pi)

1. Clone or copy the offloader directory to your Raspberry Pi
2. Run the installation script:

```bash
cd offloader
sudo ./install.sh
```

This will:
- Install Python dependencies (Flask, Flask-SocketIO)
- Create systemd service for auto-start on boot
- Start the offloader service

## Manual Installation

### Install Dependencies

```bash
pip3 install -r requirements.txt
```

### Install Service

```bash
sudo cp offloader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable offloader.service
sudo systemctl start offloader.service
```

### Check Status

```bash
sudo systemctl status offloader.service
```

### View Logs

```bash
sudo journalctl -u offloader.service -f
```

## Access

Open a browser to:
- http://offloader.local:8080 (if mDNS/Avahi is configured)
- http://[raspberry-pi-ip]:8080

## Troubleshooting

### Service won't start

Check logs:
```bash
sudo journalctl -u offloader.service -n 50
```

### Restart service

```bash
sudo systemctl restart offloader.service
```

### Stop service

```bash
sudo systemctl stop offloader.service
```

### Disable auto-start

```bash
sudo systemctl disable offloader.service
```
