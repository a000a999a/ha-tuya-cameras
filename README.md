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

## Tested hardware

| Category | Tested devices |
|---|---|
| Fixed indoor camera | Smart Camera |
| 360° panoramic camera | Panoramic / Panorama Bullet 2S |
| Outdoor bullet camera | Bullet 2S |
| Video doorbell | Intelligent Doorbell |

Running across **16 cameras**, **3 separate Tuya developer projects / SmartLife accounts**, and **4 physical sites**. None of this is brand-specific — any Tuya-ecosystem IPC camera emitting the standard `movement_detect_pic` / `initiative_message` DPS events over the developer-platform MQTT broker should work, regardless of badge.

> Cameras using Tuya's `v4.0 initiative_message` payload format (`keyId: "default"`) can't have their inline motion thumbnail decrypted — that key isn't exposed by any Tuya Open API, so it's a platform limitation rather than a per-camera bug. Those cameras still alert correctly via the RTSP snapshot fallback.

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

## Additional options (Settings → Devices & Services → Tuya Cameras → Configure)

Everything below is optional and can be changed any time after initial setup — none
of it is required to get motion alerts + SD monitoring working.

### General settings

| Field | Default | Description |
|---|---|---|
| Device list refresh interval | 14 days | How often the camera/area list is re-pulled from Tuya Cloud |
| SD card alert threshold | 90% | Usage % that triggers a tech alert email |

### AI Detection (optional — requires [ha-tuya-ai-detection](https://github.com/a000a999a/ha-tuya-ai-detection))

Without this, every motion event emails a human. With it, only events where a human
is actually detected in the frame get emailed — everything else is discarded
silently (or sent as a fail-open plain image if the AI service errors).

| Field | Default | Description |
|---|---|---|
| Enable AI detection | Off | Filter motion events through the inference service before emailing |
| Inference service URL | `http://localhost:8000` | Base URL of the running `ha-tuya-ai-detection` service |

Saving validates the URL against the service's `/health` endpoint before accepting it.

### Animal detection (optional, per camera)

Configure per camera, under **Configure → Animal detection → pick a camera**:

| Field | Default | Description |
|---|---|---|
| Enable animal detection | Off | Also alert (in addition to human detection) when a configured species is seen |
| Species | none selected | One or more COCO classes: bird, cat, dog, horse, sheep, cow, elephant, bear, zebra, giraffe — leave empty to match any animal |

Requires AI Detection to be enabled above (same inference service, same COCO model).

### Motion alert sources

| Field | Default | Description |
|---|---|---|
| Enable MQTT motion alerts | On | The primary motion path — push events from Tuya's MQTT broker |
| Enable SmartLife webhook alerts | Off | A secondary path for cameras whose MQTT delivery is unreliable — see below |

Turning MQTT alerts off only skips the *email* — AI stats and snapshots for that
camera keep recording either way.

#### SmartLife webhook bridge (optional, advanced)

Some camera models deliver Tuya MQTT motion events unreliably even though the
official SmartLife app's push notifications always fire. The webhook bridge is a
workaround for exactly those cameras: a small Android app running as a background
service on a spare Android device (an old phone or tablet works fine, kept
permanently on Wi-Fi and charging) intercepts SmartLife's own push notifications and
forwards the captured image to Home Assistant over your local network.

**Only set this up if you have a specific camera with confirmed unreliable MQTT
delivery.** For every other camera, MQTT alone is simpler and sufficient — and a
camera that already gets alerts via MQTT is automatically skipped by the webhook
path if both happen to be enabled, so there's no double-alerting risk in trying it.

1. Enable **SmartLife webhook alerts** above — the webhook URL is shown on that screen
   (`http://<your-ha-host>:8123/api/webhook/<random-id>`, also logged at INFO when the
   bridge starts); note it, you'll enter it in the app. The ID is generated randomly
   once per install and is the webhook's only secret, so don't share it. The webhook
   only accepts POSTs from your local network.
2. Build or obtain the SmartLife Bridge Android app (source alongside this repo) and
   install it on the spare device via `adb install`.
3. Open the app once and paste in the webhook URL from step 1, then save.
4. On the device: **Settings → Apps → SmartLife Bridge → Battery → Unrestricted**
   (the plain "allow background" toggle alone is not enough — LineageOS and stock
   Android's battery manager will still kill the service under this setting).
5. **Settings → Special app access → Notification access → SmartLife Bridge → On.**
6. **Reboot the device once** after granting notification access — some Android
   versions require a reboot for the notification listener to actually bind.
7. Make sure the SmartLife app itself has push notifications enabled for the
   camera(s) you want covered.

**Upgrading from 0.11.x:** the webhook used to live at the fixed address
`/api/webhook/smartlife_motion`. From 0.12.0 it has a random ID instead, and the old
address no longer works. After upgrading, open **Configure → Motion alert sources**,
copy the new URL and paste it into the SmartLife Bridge app on the device. Until you
do, webhook alerts stop arriving (MQTT alerts are unaffected).

If notifications stop arriving after some time, check that the battery-optimisation
exemption is still set (some manufacturers' custom Android builds silently reset it
on OS updates) and that the device still has notification access granted.

### Self-healing MQTT connection

The MQTT bridge (the primary motion-alert path) monitors its own connection health
and recovers automatically without needing a manual reload, on an escalating
schedule checked every 5 minutes:

| Condition | Action |
|---|---|
| Connected, but no messages for over an hour | Forces a reconnect — fixes a "connected but silent" session (the most common failure mode; recovers most events, no manual step) |
| Still silent 2 hours in, or disconnected over an hour, or the bridge's own background task died | Fully reloads the integration entry — the same fix an admin would do by hand |
| ~15 minutes after a forced reload | Checks whether messages resumed; alerts only if they haven't |

You'll get a tech alert email before any reload attempt, and again if a reload
didn't fix it — never a "recovered successfully" spam email. A reload won't be
attempted more than once every 6 hours for the same entry, to avoid thrashing; past
that, you'll get an alert asking for a manual look instead.

This has fired for real in production (not just tested) — the forced-reconnect
tier specifically. If you see it firing repeatedly for the *same* camera project on
a recurring schedule rather than as an occasional one-off, that usually just means
that project's devices are genuinely offline (see **Pause watchdog alerts** below) —
but if you know they aren't, it's worth investigating as its own issue: something
upstream (session/credential handling, network path) rather than a problem this
watchdog itself can fix.

#### Pause watchdog alerts (site deliberately offline)

If a project's devices are intentionally taken offline for a while — a site's router
unplugged on purpose, hardware pulled for maintenance — the watchdog above has no way
to tell "deliberately offline" apart from "broken," and will otherwise keep nudging
and alerting every hour indefinitely. **Configure → Pause watchdog alerts** lets you
tell it to stand down for that project specifically, for 30/60/90 days (or switch
back to Off to resume immediately, any time). While paused, the watchdog skips its
health check entirely for that entry — no reconnect/reload attempts, no tech-alert
emails. It does **not** affect motion, SD, or any other alert type, and resumes
automatically once the chosen period elapses.

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

### Local recording on human detection
Records a video clip via `camera.record` when AI confirms a human — never on raw,
unfiltered motion, so a false positive never burns a clip. Applies to both the MQTT
and ONVIF motion paths. Configure under Settings → Devices & Services → Tuya Cameras
→ Configure → "Local recording on human detection": enable/disable, clip duration,
storage path, and retention period. A separate housekeeping job runs on its own
6-hour schedule, deleting recordings past the retention period and, if disk space
still runs low afterwards, pruning the oldest remaining files and sending a tech
alert. Videos are referenced by file path in the alert email, never attached.

**Storage location:** recordings are written under `/config/www` (e.g.
`/config/www/tuya_cameras/recordings` by default), not the media-source "local"
root. This isn't an arbitrary choice — it's the only local path Home Assistant's
own `camera.record` service (`stream.async_record()`) will write to without
extra setup. HA enforces its own security allowlist independent of filesystem
permissions; straight from `core_config.py`:

```python
hac.allowlist_external_dirs = {hass.config.path("www"), *hac.media_dirs.values()}
```

Only two locations are auto-allowed with zero configuration: `media_dirs["local"]`
(which on a Docker install defaults to `/media` — typically not bind-mounted to
a host path, so anything written there is lost the moment the container is
*recreated*, not just restarted) and `<config>/www/`. `/config` is guaranteed to
be a real, host-persistent, already-bind-mounted directory on every HA install
type (Docker, HAOS, Supervised, Core), and `www/` is served by HA at `/local/...`
too, so recordings land somewhere durable and browsable with zero extra setup —
no Docker volume, no `configuration.yaml` edit, no HACS dependency. If you'd
rather store recordings elsewhere, add the target directory to
`allowlist_external_dirs` in `configuration.yaml` yourself; the storage-path
field is otherwise always resolved relative to `/config/www`.

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

**New cameras show no SD or status:** Press **Refresh All Cameras**. If still
empty, the camera name in SmartLife must match the picture-glance card title exactly
(case-insensitive) for automatic entity wiring.
