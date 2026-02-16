#!/usr/bin/env python3
"""
Footage Offloader v2 — Raspberry Pi 4 dedicated transfer appliance
Transfers files from USB-C SSD to NAS via SMB over Tailscale or LAN.

Key features:
- Transfers run server-side, independent of browser connection
- Discord webhook notifications at milestones (25/50/75/100%)
- Reconnect to active transfer from any device
- Speed + ETA tracking
"""

import os
import json
import shutil
import subprocess
import hashlib
import threading
import time
import signal
import sys
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = "/etc/offloader"
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
HISTORY_FILE = os.path.join(CONFIG_DIR, "history.json")
USB_MOUNT = "/mnt/offloader/usb"
NAS_MOUNT = "/mnt/offloader/nas"
CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB chunks for large video files

DEFAULT_CONFIG = {
    "nas_ip": "100.109.23.38",
    "nas_ip_local": "192.168.88.20",
    "share_name": "archive",
    "subfolder": "",
    "smb_username": "",
    "smb_password": "",
    "smb_version": "3.0",
    "verify_checksums": False,
    "use_tailscale": True,
    "discord_webhook": "",
    "discord_notify_milestones": [25, 50, 75, 100],
}

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = "offloader-secret-key"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ---------------------------------------------------------------------------
# Persistent transfer state (survives browser disconnects)
# ---------------------------------------------------------------------------
transfer_state = {
    "active": False,
    "cancel_requested": False,
    "started_at": None,
    "total_files": 0,
    "completed_files": 0,
    "current_file": "",
    "current_file_index": 0,
    "current_file_percent": 0.0,
    "total_bytes": 0,
    "bytes_done": 0,
    "overall_percent": 0.0,
    "speed_bps": 0,
    "eta_seconds": 0,
    "errors": [],
    "destination": "",
    "file_list": [],          # Names of files being transferred
    "completed_list": [],     # Names of completed files
    "milestones_sent": set(), # Discord milestones already sent
    "finished": False,        # True when complete (for showing result on reconnect)
    "finish_summary": None,   # Summary dict for completion screen
}

# Global drive/NAS state
drive_state = {
    "drive": None,
    "drive_mounted": False,
    "nas_mounted": False,
    "files": [],
}

# Speed tracking
speed_tracker = {
    "samples": [],  # list of (timestamp, bytes_done)
    "window": 5,    # seconds for rolling average
}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def load_config():
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            if k not in cfg:
                cfg[k] = v
        return cfg
    except Exception:
        return DEFAULT_CONFIG.copy()


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Transfer history
# ---------------------------------------------------------------------------
MAX_HISTORY = 50

def load_history():
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_history(history):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history[-MAX_HISTORY:], f, indent=2)


def add_history_entry(entry):
    history = load_history()
    history.append(entry)
    save_history(history)
    return history


# ---------------------------------------------------------------------------
# Discord notifications
# ---------------------------------------------------------------------------
def send_discord(webhook_url, message):
    """Send a message to a Discord webhook. Non-blocking, fire-and-forget."""
    if not webhook_url:
        return
    def _send():
        try:
            payload = json.dumps({"content": message}).encode("utf-8")
            req = urllib.request.Request(
                webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"Discord webhook error: {e}")
    threading.Thread(target=_send, daemon=True).start()


def format_duration(seconds):
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s}s"
    else:
        h, rem = divmod(int(seconds), 3600)
        m = rem // 60
        return f"{h}h {m}m"


def format_eta(seconds):
    if seconds <= 0:
        return "almost done"
    return f"{format_duration(seconds)} remaining"


def check_milestones(config):
    """Check if we crossed a Discord notification milestone."""
    pct = transfer_state["overall_percent"]
    milestones = config.get("discord_notify_milestones", [25, 50, 75, 100])
    webhook = config.get("discord_webhook", "")

    if not webhook:
        return

    for m in milestones:
        if pct >= m and m not in transfer_state["milestones_sent"]:
            transfer_state["milestones_sent"].add(m)

            total_h = human_size(transfer_state["total_bytes"])
            done_h = human_size(transfer_state["bytes_done"])
            eta = format_eta(transfer_state["eta_seconds"])
            dest = transfer_state["destination"]
            files_done = transfer_state["completed_files"]
            files_total = transfer_state["total_files"]
            errors = len(transfer_state["errors"])

            if m == 100:
                elapsed = time.time() - transfer_state["started_at"]
                avg_speed = transfer_state["total_bytes"] / elapsed if elapsed > 0 else 0
                err_msg = f" — {errors} error(s)" if errors else ""
                msg = (
                    f"✅ **Transfer complete**\n"
                    f"📁 {files_total} files — {total_h}{err_msg}\n"
                    f"📍 `{dest}`\n"
                    f"⏱ Duration: {format_duration(elapsed)} — Avg: {human_size(avg_speed)}/s"
                )
            elif m == 0:
                msg = (
                    f"🚀 **Transfer started**\n"
                    f"📁 {files_total} files — {total_h}\n"
                    f"📍 `{dest}`"
                )
            else:
                msg = (
                    f"📊 **{m}% complete**\n"
                    f"📁 {files_done}/{files_total} files — {done_h} / {total_h}\n"
                    f"⏱ {eta}"
                )

            send_discord(webhook, msg)


# ---------------------------------------------------------------------------
# Speed + ETA calculation
# ---------------------------------------------------------------------------
def update_speed():
    """Update rolling average speed and ETA."""
    now = time.time()
    speed_tracker["samples"].append((now, transfer_state["bytes_done"]))

    # Prune old samples
    cutoff = now - speed_tracker["window"]
    speed_tracker["samples"] = [
        s for s in speed_tracker["samples"] if s[0] >= cutoff
    ]

    if len(speed_tracker["samples"]) >= 2:
        oldest = speed_tracker["samples"][0]
        dt = now - oldest[0]
        db = transfer_state["bytes_done"] - oldest[1]
        if dt > 0:
            bps = db / dt
            transfer_state["speed_bps"] = bps
            remaining = transfer_state["total_bytes"] - transfer_state["bytes_done"]
            transfer_state["eta_seconds"] = remaining / bps if bps > 0 else 0


# ---------------------------------------------------------------------------
# USB drive detection & mounting
# ---------------------------------------------------------------------------
def find_usb_drives():
    try:
        r = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,TRAN,MODEL,FSTYPE"],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(r.stdout)
        drives = []
        for dev in data.get("blockdevices", []):
            if dev.get("tran") != "usb":
                continue
            children = dev.get("children", [])
            targets = children if children else [dev]
            for part in targets:
                if part.get("type") not in ("part", "disk"):
                    continue
                drives.append({
                    "device": f"/dev/{part['name']}",
                    "size": part.get("size", "?"),
                    "model": (dev.get("model") or "USB Drive").strip(),
                    "fstype": part.get("fstype", ""),
                    "mountpoint": part.get("mountpoint"),
                })
        return drives
    except Exception:
        return []


def mount_usb(device):
    os.makedirs(USB_MOUNT, exist_ok=True)
    subprocess.run(["umount", USB_MOUNT], capture_output=True)
    r = subprocess.run(
        ["mount", "-o", "ro", device, USB_MOUNT],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False, r.stderr
    return True, ""


def unmount_usb():
    subprocess.run(["umount", "-l", USB_MOUNT], capture_output=True)
    drive_state["drive_mounted"] = False
    drive_state["drive"] = None
    drive_state["files"] = []


# ---------------------------------------------------------------------------
# NAS SMB mounting
# ---------------------------------------------------------------------------
def mount_nas(config):
    os.makedirs(NAS_MOUNT, exist_ok=True)
    subprocess.run(["umount", "-l", NAS_MOUNT], capture_output=True)

    ip = config["nas_ip"] if config.get("use_tailscale") else config.get("nas_ip_local", config["nas_ip"])
    share = f"//{ip}/{config['share_name']}"

    opts = f"vers={config.get('smb_version', '3.0')}"
    if config.get("smb_username"):
        opts += f",username={config['smb_username']},password={config['smb_password']}"
    else:
        opts += ",guest"
    opts += ",uid=0,gid=0,file_mode=0777,dir_mode=0777"

    r = subprocess.run(
        ["mount", "-t", "cifs", share, NAS_MOUNT, "-o", opts],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False, r.stderr.strip()
    return True, ""


def unmount_nas():
    subprocess.run(["umount", "-l", NAS_MOUNT], capture_output=True)
    drive_state["nas_mounted"] = False


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------
def human_size(nbytes):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


MIN_FILE_SIZE = 1024 * 1024  # 1 MB — skip tiny system/metadata files

HIDDEN_DIRS = {
    ".Spotlight-V100", ".fseventsd", ".Trashes", ".TemporaryItems",
    ".DS_Store", "._.Trashes", ".journal", ".VolumeIcon.icns",
    "System Volume Information", "$RECYCLE.BIN", "RECYCLER",
}


def scan_files(root):
    files = []
    root = Path(root)
    try:
        for fp in sorted(root.rglob("*")):
            # Skip hidden files/dirs (dotfiles) and known system directories
            parts = fp.relative_to(root).parts
            if any(p.startswith(".") or p in HIDDEN_DIRS for p in parts):
                continue
            if fp.is_file():
                st = fp.stat()
                if st.st_size < MIN_FILE_SIZE:
                    continue
                files.append({
                    "name": str(fp.relative_to(root)),
                    "size": st.st_size,
                    "size_human": human_size(st.st_size),
                })
    except Exception:
        pass
    return files


# ---------------------------------------------------------------------------
# File transfer (runs server-side, independent of browser)
# ---------------------------------------------------------------------------
def transfer_worker(selected_files, config):
    """Background thread: copy selected files from USB → NAS.

    This runs entirely server-side. Browser can disconnect and reconnect
    at any time — the transfer state is always available.
    """
    # Reset state
    transfer_state["active"] = True
    transfer_state["cancel_requested"] = False
    transfer_state["started_at"] = time.time()
    transfer_state["errors"] = []
    transfer_state["completed_list"] = []
    transfer_state["milestones_sent"] = set()
    transfer_state["finished"] = False
    transfer_state["finish_summary"] = None
    speed_tracker["samples"] = []

    dest_base = Path(NAS_MOUNT)
    if config.get("subfolder"):
        dest_base = dest_base / config["subfolder"]

    ip = config["nas_ip"] if config.get("use_tailscale") else config.get("nas_ip_local", config["nas_ip"])
    transfer_state["destination"] = f"//{ip}/{config['share_name']}" + (
        f"/{config['subfolder']}" if config.get("subfolder") else ""
    )

    # Map selected names → file info
    file_map = {f["name"]: f for f in drive_state["files"]}
    to_copy = [file_map[n] for n in selected_files if n in file_map]

    total_size = sum(f["size"] for f in to_copy)
    transfer_state["total_files"] = len(to_copy)
    transfer_state["total_bytes"] = total_size
    transfer_state["bytes_done"] = 0
    transfer_state["overall_percent"] = 0
    transfer_state["file_list"] = [f["name"] for f in to_copy]

    # Send start notification
    transfer_state["milestones_sent"].add(0)
    send_discord(
        config.get("discord_webhook", ""),
        f"🚀 **Transfer started**\n"
        f"📁 {len(to_copy)} files — {human_size(total_size)}\n"
        f"📍 `{transfer_state['destination']}`"
    )

    socketio.emit("transfer_started", {
        "total_files": len(to_copy),
        "total_size": total_size,
        "total_size_human": human_size(total_size),
    })

    for i, finfo in enumerate(to_copy):
        if transfer_state["cancel_requested"]:
            socketio.emit("transfer_cancelled", {})
            break

        src = Path(USB_MOUNT) / finfo["name"]
        dst = dest_base / Path(finfo["name"]).name  # Just filename, no folder structure
        dest_base.mkdir(parents=True, exist_ok=True)

        transfer_state["current_file"] = finfo["name"]
        transfer_state["current_file_index"] = i
        transfer_state["current_file_percent"] = 0

        socketio.emit("file_started", {
            "index": i,
            "name": finfo["name"],
            "size_human": finfo["size_human"],
        })

        try:
            file_done = 0
            last_emit = 0
            with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
                while True:
                    if transfer_state["cancel_requested"]:
                        break
                    chunk = fsrc.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    fdst.write(chunk)
                    file_done += len(chunk)
                    transfer_state["bytes_done"] += len(chunk)

                    file_pct = (file_done / finfo["size"] * 100) if finfo["size"] else 100
                    overall_pct = (transfer_state["bytes_done"] / total_size * 100) if total_size else 100

                    transfer_state["current_file_percent"] = file_pct
                    transfer_state["overall_percent"] = overall_pct

                    now = time.time()
                    if now - last_emit > 0.3:
                        last_emit = now
                        update_speed()
                        check_milestones(config)

                        socketio.emit("file_progress", {
                            "index": i,
                            "name": finfo["name"],
                            "file_percent": file_pct,
                            "overall_percent": overall_pct,
                            "completed_files": transfer_state["completed_files"],
                            "total_files": transfer_state["total_files"],
                            "bytes_done": transfer_state["bytes_done"],
                            "speed_bps": transfer_state["speed_bps"],
                            "speed_human": human_size(transfer_state["speed_bps"]) + "/s",
                            "eta_seconds": transfer_state["eta_seconds"],
                            "eta_human": format_eta(transfer_state["eta_seconds"]),
                        })

            if transfer_state["cancel_requested"]:
                dst.unlink(missing_ok=True)
                continue

            # Copy metadata
            shutil.copystat(str(src), str(dst))

            # Verify checksum if enabled
            if config.get("verify_checksums"):
                transfer_state["current_file"] = f"Verifying: {finfo['name']}"
                socketio.emit("file_verifying", {"index": i, "name": finfo["name"]})

                src_hash = md5_file(str(src))
                dst_hash = md5_file(str(dst))
                if src_hash != dst_hash:
                    transfer_state["errors"].append(finfo["name"])
                    socketio.emit("file_error", {
                        "index": i, "name": finfo["name"], "error": "Checksum mismatch",
                    })
                    continue

            transfer_state["completed_files"] = i + 1
            transfer_state["completed_list"].append(finfo["name"])

            socketio.emit("file_complete", {
                "index": i,
                "name": finfo["name"],
                "overall_percent": transfer_state["overall_percent"],
            })

        except Exception as e:
            transfer_state["errors"].append(finfo["name"])
            socketio.emit("file_error", {
                "index": i, "name": finfo["name"], "error": str(e),
            })

    # Finalize
    transfer_state["active"] = False

    if not transfer_state["cancel_requested"]:
        transfer_state["overall_percent"] = 100
        transfer_state["finished"] = True

        elapsed = time.time() - transfer_state["started_at"]
        avg_speed = total_size / elapsed if elapsed > 0 else 0

        transfer_state["finish_summary"] = {
            "total_files": len(to_copy),
            "total_size_human": human_size(total_size),
            "errors": transfer_state["errors"],
            "duration": format_duration(elapsed),
            "avg_speed": human_size(avg_speed) + "/s",
            "destination": transfer_state["destination"],
        }

        # Final milestone check (100%)
        check_milestones(config)

        # Save to transfer history
        history_entry = {
            "title": config.get("subfolder") or "untitled",
            "date": datetime.now().strftime("%b %d"),
            "time": datetime.now().strftime("%I:%M %p"),
            "duration": format_duration(elapsed),
            "total_size": human_size(total_size),
            "avg_speed": human_size(avg_speed) + "/s",
            "total_files": len(to_copy),
            "errors": len(transfer_state["errors"]),
            "timestamp": time.time(),
            "file_names": [f["name"] for f in to_copy],
        }
        history = add_history_entry(history_entry)
        transfer_state["finish_summary"]["history"] = history

        socketio.emit("transfer_complete", transfer_state["finish_summary"])


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Background: poll for USB drive changes
# ---------------------------------------------------------------------------
def drive_monitor():
    prev_drives = set()
    while True:
        try:
            drives = find_usb_drives()
            current = {d["device"] for d in drives}

            if current - prev_drives:
                new_devs = current - prev_drives
                for d in drives:
                    if d["device"] in new_devs:
                        ok, err = mount_usb(d["device"])
                        if ok:
                            drive_state["drive"] = d
                            drive_state["drive_mounted"] = True
                            drive_state["files"] = scan_files(USB_MOUNT)
                            socketio.emit("drive_connected", {
                                "drive": d, "files": drive_state["files"],
                            })
                        else:
                            socketio.emit("drive_error", {"device": d["device"], "error": err})
                        break

            if prev_drives - current and drive_state["drive_mounted"]:
                unmount_usb()
                socketio.emit("drive_disconnected", {})

            prev_drives = current
        except Exception:
            pass
        time.sleep(2)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/preview")
def preview():
    return render_template("preview-desktop.html")


@app.route("/api/status")
def api_status():
    """REST endpoint for quick status check (e.g., from scripts)."""
    return jsonify(get_full_state())


def get_full_state():
    cfg = load_config()
    return {
        "drive": drive_state["drive"],
        "drive_mounted": drive_state["drive_mounted"],
        "nas_mounted": drive_state["nas_mounted"],
        "files": drive_state["files"],
        "config": {k: v for k, v in cfg.items() if k != "smb_password"},
        "config_has_password": bool(cfg.get("smb_password")),
        "history": load_history(),
        # Transfer state (always present so reconnecting clients get current progress)
        "transfer": {
            "active": transfer_state["active"],
            "finished": transfer_state["finished"],
            "total_files": transfer_state["total_files"],
            "completed_files": transfer_state["completed_files"],
            "current_file": transfer_state["current_file"],
            "current_file_index": transfer_state["current_file_index"],
            "current_file_percent": transfer_state["current_file_percent"],
            "total_bytes": transfer_state["total_bytes"],
            "bytes_done": transfer_state["bytes_done"],
            "overall_percent": transfer_state["overall_percent"],
            "speed_bps": transfer_state["speed_bps"],
            "speed_human": human_size(transfer_state["speed_bps"]) + "/s" if transfer_state["speed_bps"] else "",
            "eta_seconds": transfer_state["eta_seconds"],
            "eta_human": format_eta(transfer_state["eta_seconds"]) if transfer_state["eta_seconds"] else "",
            "errors": transfer_state["errors"],
            "destination": transfer_state["destination"],
            "file_list": transfer_state["file_list"],
            "completed_list": transfer_state["completed_list"],
            "finish_summary": transfer_state["finish_summary"],
        },
    }


# ---------------------------------------------------------------------------
# Socket.IO events
# ---------------------------------------------------------------------------
@socketio.on("connect")
def on_connect():
    """Send FULL state on new connection (handles reconnect mid-transfer)."""
    emit("status", get_full_state())


@socketio.on("save_config")
def on_save_config(data):
    cfg = load_config()
    for key in ("nas_ip", "nas_ip_local", "share_name", "subfolder",
                "smb_username", "smb_password", "smb_version",
                "verify_checksums", "use_tailscale",
                "discord_webhook", "discord_notify_milestones"):
        if key in data:
            cfg[key] = data[key]
    save_config(cfg)
    emit("config_saved", {
        "config": {k: v for k, v in cfg.items() if k != "smb_password"},
        "config_has_password": bool(cfg.get("smb_password")),
    })


@socketio.on("connect_nas")
def on_connect_nas():
    cfg = load_config()
    ok, err = mount_nas(cfg)
    drive_state["nas_mounted"] = ok
    if ok:
        emit("nas_connected", {})
    else:
        emit("nas_error", {"error": err})


@socketio.on("disconnect_nas")
def on_disconnect_nas():
    unmount_nas()
    emit("nas_disconnected", {})


@socketio.on("rescan_drive")
def on_rescan():
    if drive_state["drive_mounted"]:
        drive_state["files"] = scan_files(USB_MOUNT)
        socketio.emit("files_updated", {"files": drive_state["files"]})
    else:
        drives = find_usb_drives()
        if drives:
            d = drives[0]
            ok, err = mount_usb(d["device"])
            if ok:
                drive_state["drive"] = d
                drive_state["drive_mounted"] = True
                drive_state["files"] = scan_files(USB_MOUNT)
                socketio.emit("drive_connected", {"drive": d, "files": drive_state["files"]})
            else:
                socketio.emit("drive_error", {"device": d["device"], "error": err})
        else:
            socketio.emit("drive_disconnected", {})


@socketio.on("start_transfer")
def on_start_transfer(data):
    if transfer_state["active"]:
        emit("error", {"message": "Transfer already in progress"})
        return
    if not drive_state["drive_mounted"]:
        emit("error", {"message": "No USB drive connected"})
        return
    if not drive_state["nas_mounted"]:
        emit("error", {"message": "NAS not connected"})
        return

    selected = data.get("files", [])
    if not selected:
        emit("error", {"message": "No files selected"})
        return

    # Clear previous finish state
    transfer_state["finished"] = False
    transfer_state["finish_summary"] = None

    cfg = load_config()
    t = threading.Thread(target=transfer_worker, args=(selected, cfg), daemon=True)
    t.start()


@socketio.on("cancel_transfer")
def on_cancel_transfer():
    transfer_state["cancel_requested"] = True


@socketio.on("clear_finished")
def on_clear_finished():
    """Clear the finished transfer state so UI returns to idle."""
    transfer_state["finished"] = False
    transfer_state["finish_summary"] = None


@socketio.on("speed_test")
def on_speed_test():
    """Write a real test file to NAS and measure throughput."""
    if not drive_state["nas_mounted"]:
        emit("speed_test_error", {"error": "NAS not connected"})
        return

    def _run():
        test_file = Path(NAS_MOUNT) / ".offloader_speedtest.tmp"
        test_size = 256 * 1024 * 1024  # 256 MB
        chunk = os.urandom(CHUNK_SIZE)  # 4 MB random data
        written = 0
        try:
            start = time.time()
            with open(test_file, "wb") as f:
                while written < test_size:
                    to_write = min(CHUNK_SIZE, test_size - written)
                    f.write(chunk[:to_write])
                    f.flush()
                    written += to_write
                    pct = written / test_size * 100
                    socketio.emit("speed_test_progress", {"percent": pct})
                os.fsync(f.fileno())
            elapsed = time.time() - start
            bps = written / elapsed if elapsed > 0 else 0
            socketio.emit("speed_test_done", {
                "bytes_per_sec": bps,
                "mbps": bps / (1024 * 1024),
                "elapsed": elapsed,
                "test_size": test_size,
            })
        except Exception as e:
            socketio.emit("speed_test_error", {"error": str(e)})
        finally:
            try:
                test_file.unlink(missing_ok=True)
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    os.makedirs(USB_MOUNT, exist_ok=True)
    os.makedirs(NAS_MOUNT, exist_ok=True)
    os.makedirs(CONFIG_DIR, exist_ok=True)

    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)

    # Check for already-connected USB drive
    drives = find_usb_drives()
    if drives:
        d = drives[0]
        ok, _ = mount_usb(d["device"])
        if ok:
            drive_state["drive"] = d
            drive_state["drive_mounted"] = True
            drive_state["files"] = scan_files(USB_MOUNT)

    # Start background drive monitor
    socketio.start_background_task(drive_monitor)

    # Run server
    socketio.run(app, host="0.0.0.0", port=8080, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
