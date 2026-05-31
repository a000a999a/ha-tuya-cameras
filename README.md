# Tuya Cameras

Real-time motion alerts and SD card monitoring for Tuya IPC cameras in Home Assistant.

Connects to Tuya's MQTT broker for instant motion notifications. Monitors SD card
usage on all cameras and alerts when cards approach full. Provides per-camera
Format SD buttons directly in HA.

## Prerequisites

- **[Tuya Home Core](https://github.com/a000a999a/ha-tuya-home-core)** installed and configured
- Tuya IPC cameras registered in your Tuya/SmartLife account
- Cameras assigned to homes in the SmartLife app (used for area grouping)
- A Gmail account with an [App Password](https://support.google.com/accounts/answer/185833) (or any SMTP server)

## Installation via HACS

1. Install **Tuya Home Core** first
2. In HACS → Custom repositories, add `a000a999a/ha-tuya-cameras`
3. Install **Tuya Cameras** and restart Home Assistant
4. Go to **Settings → Devices & Services → Add Integration → Tuya Cameras**

## Configuration

### Step 1 — Select Tuya account
Auto-selected if only one Tuya Home Core entry exists.

### Step 2 — SMTP settings

| Field | Description |
|---|---|
| SMTP host | e.g. `smtp.gmail.com` |
| SMTP port | e.g. `587` (TLS) |
| Sender email | Address emails are sent from |
| Password | App Password (Gmail) or SMTP password |

### Step 3+ — Recipients per area

For each area discovered from your Tuya homes, configure:

| Field | Description |
|---|---|
| Human alert recipients | Emails notified on motion (semicolon-separated) |
| Tech alert recipients  | Emails notified for SD card alerts (semicolon-separated) |

Leave blank to disable alerts for that area.

### Updating settings
Go to **Settings → Devices & Services → Tuya Cameras → Configure**.

## Features

### Real-time motion alerts
The integration connects to Tuya's MQTT broker and receives motion events
within seconds. For each event it attempts to download the motion thumbnail
from Tuya OSS and includes it in the alert email.

### SD card monitoring
Every 15 minutes, the integration polls SD card status for all cameras.
A tech alert email is sent if any camera's SD usage reaches **90%**.
The threshold is not currently configurable in the UI (raise an issue to request it).

### Format SD card
Each camera gets a **Format SD Card** button in HA. Press to trigger an immediate
format via the Tuya API.

### Camera status sensors
Each camera exposes:
- `sensor.<name>_sd_usage` — SD usage percentage
- `sensor.<name>_status` — online / offline

## Human Detection (AI extension)

AI-powered human detection using YOLOv8 is not included in this release but is
fully documented. See **[docs/yolo_extension.md](docs/yolo_extension.md)**.

## Supported camera categories

`sp`, `ipc`, `dh`, `nvr`, `sp-new` — all standard Tuya IPC categories.

## Services

| Service | Description |
|---|---|
| `tuya_cameras.refresh_status` | Force immediate status refresh |
| `tuya_cameras.format_sd` | Format SD card by device ID |

## Troubleshooting

**No cameras found:** Check that cameras are assigned to homes in the SmartLife app
and that your Tuya API key has Smart Home API access enabled.

**MQTT not connecting:** Ensure your Tuya project has MQTT/messaging API enabled
on the iot.tuya.com project page.

**Emails not sending:** Gmail requires an App Password — your regular password
will not work. Enable 2FA first, then create an App Password.
