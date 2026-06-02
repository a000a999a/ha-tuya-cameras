# Tuya Cameras

Real-time motion alerts, SD card monitoring, and Format SD buttons for Tuya IPC cameras in Home Assistant.

Connects to Tuya's MQTT broker for instant push motion notifications. Works alongside
the official **Tuya** HA integration — which provides live video streams — to give
a complete camera dashboard with video, SD status, and email alerts.

---

## How the two Tuya systems work together

| What you need | Where it comes from |
|---|---|
| Live video stream (`camera.xxx`) | Official **Tuya** integration (hub) |
| SD card usage sensor | This integration (Tuya Cameras) |
| Camera online/offline status | This integration (Tuya Cameras) |
| Format SD Card button | This integration (Tuya Cameras) |
| MQTT motion alert email | This integration (Tuya Cameras) |

Both are required for a complete setup. See the
[Tuya Home Core README](https://github.com/a000a999a/ha-tuya-home-core) for the
full six-step setup guide starting from camera registration in SmartLife.

---

## Prerequisites

- **[Tuya Home Core](https://github.com/a000a999a/ha-tuya-home-core)** installed and configured
- **Official Tuya integration** configured in HA (for live video streams)
- Tuya IPC cameras registered in SmartLife and assigned to homes
- A Gmail account with an [App Password](https://support.google.com/accounts/answer/185833) (or any SMTP server)

## Installation via HACS

1. Complete the [full setup guide](https://github.com/a000a999a/ha-tuya-home-core#full-setup-guide) in Tuya Home Core first
2. In HACS → Custom repositories, add `a000a999a/ha-tuya-cameras`
3. Install **Tuya Cameras** and restart Home Assistant
4. Go to **Settings → Devices & Services → Add Integration → Tuya Cameras**

## Multiple Tuya projects

If cameras are split across multiple Tuya developer projects (e.g. to stay under the 50-device limit), add one **Tuya Cameras** entry per project — each runs its own independent MQTT bridge. The **Refresh All Cameras** button covers all projects at once.

## Configuration

### Step 1 — Select Tuya account

If only one Tuya Home Core entry exists it is auto-selected. If multiple exist, a dropdown shows each entry with its project label, areas, and device count so you can distinguish them.

### Step 2 — SMTP settings

| Field | Description |
|---|---|
| SMTP host | e.g. `smtp.gmail.com` |
| SMTP port | e.g. `587` (TLS) |
| Sender email | Address emails are sent from |
| Password | App Password (Gmail) or SMTP password |

### Step 3+ — Recipients per area

For each area (Tuya home name), configure:

| Field | Description |
|---|---|
| Human alert recipients | Emails notified on human motion (semicolon-separated) |
| Tech alert recipients  | Emails notified for SD card alerts (semicolon-separated) |

Leave blank to disable alerts for that area.

## Refresh All Cameras

A single service call `tuya_cameras.refresh_all` refreshes all projects and updates
the Lovelace Cameras view automatically. Add this button card to any dashboard:

```yaml
type: button
name: Refresh All Cameras
icon: mdi:refresh
tap_action:
  action: call-service
  service: tuya_cameras.refresh_all
```

When pressed:
1. Pulls the updated device list from each linked Tuya project
2. Re-polls SD card status for all cameras
3. Fills in entities (SD usage · Status · Format SD Card) for any picture-glance cards whose title matches a camera name

If new cameras are discovered (not present before the refresh), the affected
integration entry reloads automatically to create their entities.

## Features

### Real-time motion alerts
Connects to Tuya's MQTT broker (push, no polling). For each motion event, downloads
the thumbnail from Tuya OSS and emails it. Falls back to an HA camera snapshot if
OSS download fails.

### SD card monitoring
Polls SD status every 15 minutes. Sends a tech alert email when usage reaches 90%.

### Format SD Card
One button per camera. Press to format via Tuya Cloud API — no confirmation prompt.

### Camera status sensors
- `sensor.<name>_sd_usage` — SD usage %
- `sensor.<name>_status` — online / offline

## Services

| Service | Scope | Description |
|---|---|---|
| `tuya_cameras.refresh_all` | All projects | Refresh device lists + SD status + update Lovelace |
| `tuya_cameras.refresh_status` | Per entry | Re-poll SD status for that entry's cameras |
| `tuya_cameras.format_sd` | Per camera | Format SD card by device ID |

## Troubleshooting

**No cameras found:** Check cameras are assigned to Tuya homes in SmartLife and
that the developer API project has Smart Home API access.

**MQTT not connecting:** Ensure MQTT/messaging API is enabled on iot.tuya.com and
BizCode subscriptions are active.

**Emails not sending:** Gmail requires an App Password — regular password won't work.
Enable 2FA first, then create an App Password at myaccount.google.com.

**Wallis / new cameras show no SD or status:** Press **Refresh All Cameras**. If still
empty, the camera name in SmartLife must match the picture-glance card title exactly
(case-insensitive) for automatic entity wiring.
