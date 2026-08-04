# CLAUDE.md — ha-tuya-cameras

Extends /home/alex/ha-projects/CLAUDE.md. Read that file first.

## This Repo's Role
Camera SD monitoring, real-time motion alerts, and AI human detection.
Two independent motion pipelines: MQTT bridge (existing, all cameras) and SmartLife webhook bridge (new, requires Android bridge app on a tablet).
Depends on tuya_home_core for Tuya credentials and area map.
AI filtering (YOLOv8n) runs in a separate Docker service; wired via AIClient.

## CRITICAL — Quota Burn Warning
The previous cron-based motion poller burned 49,500 getdevicelog API calls in 2.5 days
against a 26,000/month quota (23× daily budget). It was disabled 2026-05-22.
- NEVER add any scheduled or polling call to getdevicelog, getdevices, or any Tuya Cloud API
  that runs more frequently than once per hour per camera.
- get_motion_events() was removed from camera_api.py for this reason — do NOT re-add it.
- Motion detection path is MQTT only (push, event-driven). No cron. No polling.
- MQTT generates ~50 API calls/month by comparison.

## Motion Event Flow (MQTT path)
1. Camera pushes DPS event (initiative_message or movement_detect_pic) via Tuya MQTT
2. Bridge decrypts payload (AES-128-ECB, key=mqtt_password[8:24])
3. Parses {devId, status[{code, value, t}]} — both DPS codes embed bucket+files
4. Camera lookup performed BEFORE DPS code filter — unknown codes from known cameras logged at DEBUG
5. Try OSS image download (images.tuyaeu.com/{bucket}/{path}) — encrypted with file_key
   For ?param= signed URLs: attempt direct HTTP GET first (may return plain JPEG); if 403 → fall through
6. If OSS fails → up to 3 HA snapshots at t=0/+2s/+4s with inline AI early-exit on first human hit
7. If no image at all → log at WARNING and discard (no email, no YOLO call)
8. Pass image to AI service → human detected: email annotated image / no human: discard
   If prefetched_ai already set from snapshot loop → skip redundant re-analysis

## SmartLife Webhook Pipeline (webhook_bridge.py)
Second motion pipeline — receives images directly from the SmartLife app via the Android bridge APK.
APK source: `/home/alex/ha-projects/smartlife-notif-bridge/` (package `com.alex.tuyabridge`).
Built APK: `ha-tuya-cameras/android/SmartLifeBridge-1.0.apk` — installed on SM-T713 Tab S2 (192.168.1.218).

**How it works:**
1. SmartLife app receives FCM push notification; attaches captured motion image as BigPictureStyle Bitmap (EXTRA_PICTURE).
2. `NotificationBridgeService` (NotificationListenerService) intercepts the notification, encodes the Bitmap as JPEG.
3. Service POSTs multipart form-data to `/api/webhook/smartlife_motion`: fields `title`, `text`, `image` (file).
4. `SmartLifeWebhookBridge._handle()` in HA receives the POST, matches camera name, runs AI, sends email.

**Why it exists:** SmartLife's native capture image is taken at detection instant by the camera's onboard AI. The MQTT RTSP snapshot path always arrives seconds late (stream not live, token expiry risk). Webhook images bypass the CDN 403 issue entirely — they come from the app's own Bitmap, not from the IP-restricted CDN.

**Camera name matching (two-pass):**
- Pass 1: exact full camera name (case-insensitive) appears anywhere in `title + text`.
- Pass 2: a distinctive word (>4 chars, not in `_GENERIC_WORDS`) from the camera name appears in search.
- `_GENERIC_WORDS = {"camera", "smart", "motion", "detected", "detect", "alert", "cam"}` — excluded to prevent "Camera Door" matching "Camera Tatuapé Garagen".
- Fallback: uses notification title as camera name, first available entry data.

**Global instance, per-entry toggle:**
- One `SmartLifeWebhookBridge` instance shared across all config entries (started when any entry enables it).
- Each entry independently enables/disables via `webhook_alerts_enabled` option (default off).
- MQTT toggle (`mqtt_alerts_enabled`, default on) skips email only — AI stats + snapshots still run.
- Both toggles in tuya_cameras → Configure → "Motion alert sources".

**AI stats:** Both pipelines call `ai_stats.async_record()` and fire `{DOMAIN}_ai_updated` — AI Detection tab tracks all events regardless of pipeline.

**Tablet setup requirements:**
1. Open SmartLife Bridge app → enter HA webhook URL → tap Save.
2. Settings → Apps → SmartLife Bridge → Battery → Unrestricted.
3. Settings → Special app access → Notification access → SmartLife Bridge → ON.
4. Reboot tablet after granting notification access (LineageOS requires rebind of the service).
5. SmartLife app must have push notifications enabled for each camera.

**Android build notes:**
- Requires JDK 21 (`/usr/lib/jvm/java-21-openjdk`) — JDK 26 incompatible with Gradle 8.7.
- AGP 8.5.2, Kotlin 2.0.0, Gradle 8.7, `android.useAndroidX=true` in gradle.properties.
- gradlew script and gradle/wrapper/gradle-wrapper.jar must both exist — jar downloadable from GitHub raw (gradle v8.7.0).
- Deploy: `adb push SmartLifeBridge-1.0.apk /data/local/tmp/ && adb shell pm install -r /data/local/tmp/SmartLifeBridge-1.0.apk`.
- After install, force-stop the old process: `adb shell am force-stop com.alex.tuyabridge` — otherwise the old binding persists and the new `onListenerConnected()` never fires.
- ADB TCP (`adb connect 192.168.1.218:5555`) must be re-enabled after each tablet reboot; USB ADB (device ID `ef2a508904249172`) is always available without re-enabling.

**Android gotchas found 2026-06-10 (all fixed in current APK):**
- `MissingForegroundServiceTypeException` — targetSdk=35 requires `android:foregroundServiceType="specialUse"` on the `<service>` element AND `<uses-permission android:name="android.permission.FOREGROUND_SERVICE_SPECIAL_USE"/>` in manifest. Without it the service crashes immediately after `onListenerConnected()` fires, then waits 30 min before retrying. Symptom: works briefly after reboot, stops 30s later.
- No `onListenerConnected()`/`onListenerDisconnected()` — without `startForeground()` in `onListenerConnected()`, Android kills the service under battery pressure. Without `requestRebind()` in `onListenerDisconnected()`, the service never reconnects after Android drops it.
- `Cleartext HTTP traffic to 192.168.1.241 not permitted` — Android 9+ blocks HTTP by default. Fixed by `android:usesCleartextTraffic="true"` on the `<application>` element. Symptom: service intercepted notifications but every POST failed silently (HTTP 200 never reached HA).
- `DataOutputStream.writeBytes()` sends Latin-1 (low byte only) — breaks accented characters in camera names (Bèrgerie → 0xE8, portão → 0xE9). Fixed by `out.write(string.toByteArray(Charsets.UTF_8))` for all text fields. Also added `Content-Type: text/plain; charset=utf-8` to each multipart field header. Symptom: HA `request.post()` threw `utf-8 codec can't decode byte 0xe8`.

## Image Decryption
- OSS images are AES-128-ECB encrypted (most Brasil/Wallis cameras)
- Use `cryptography` library (in requirements) — NOT pycryptodome (not installed in HA)
- file_key from DPS payload: hex string (32 chars → bytes.fromhex) or ASCII padded to 16 bytes

## Universal Coverage Rule
Every fix to the MQTT motion pipeline must be validated against ALL snapshot-path camera types:
- Brasil v4.0 initiative_message — always snapshot (no OSS)
- Brasil movement_detect_pic ?param= CDN — always snapshot (403 on CDN)
- Wallis v4.0 initiative_message — always snapshot (no OSS)
- Winterthur Camera Door movement_detect_pic ?param= — always snapshot
- Winterthur other cameras (clean key) — OSS usually works; snapshot is fallback
Cameras that get OSS images are unaffected by snapshot-path fixes (correct by design).
When adding any logic inside the snapshot loop: verify it uses no variables defined after the loop.

Webhook pipeline coverage: all three account/entry types are covered by `_find_camera()` — validated live 2026-06-10:
- Winti/Camera Door (conf=0.91 human → email sent)
- Winti/Smart Camera (conf=0.88–0.92 human → email sent)
- Lehner Wallis/Cage (image received, no human → discarded)
- Brasil/Camera Tatuapé Garagen (image received, no human → discarded)
- Brasil/Camera Tatuapé portão (conf=0.82 human → email sent; accented ã parsed correctly after UTF-8 fix)

## Checklist Additions
- [ ] NEVER call gw.status() anywhere in this repo
- [ ] NEVER add getdevicelog, getdevices, or any polling loop to motion detection
- [ ] coordinator polls SD status — do not add camera discovery to the coordinator poll cycle
- [ ] mqtt_bridge.py must track expire_time and reconnect 10 min before expiry
- [ ] mqtt_bridge.py decrypt key = password[8:24] — this changes every session
- [ ] MQTT credentials fetched once on connect, not per-message
- [ ] sensor unique_id = config_entry.entry_id + device_id + sensor_type
- [ ] button unique_id = config_entry.entry_id + device_id + "format_sd"
- [ ] notify.py must never raise — catch all SMTP exceptions and log them
- [ ] Per-area recipients come from options entry only — never hardcoded
- [ ] A detection with zero resolved recipients (empty/unmapped camera area) must WARN, never fail silently — both `mqtt_bridge.py` and `webhook_bridge.py` do this; keep them in sync
- [ ] `coordinator.py`'s `_cameras_from_registry()` must WARN when a camera has no Area assigned — this is the root cause that makes the recipients warning above fire in the first place
- [ ] OSS image download attempted before HA snapshot fallback
- [ ] For ?param= signed URLs: try direct HTTP GET first, fall back to snapshot only on failure
- [ ] Multi-snapshot loop (t=0/+2s/+4s) runs AI inline — do NOT add extra AI calls after the loop
- [ ] Camera lookup must happen BEFORE the DPS code filter (preserves unknown-code logging for known cameras)
- [ ] Image decryption uses cryptography library, not Crypto/pycryptodome
- [ ] webhook_bridge.py — _find_camera() must use two-pass matching (exact full name, then distinctive words); never skip _GENERIC_WORDS check
- [ ] Webhook toggle (webhook_alerts_enabled) must default to False — do not change this default
- [ ] MQTT toggle (mqtt_alerts_enabled) must default to True — skips email only, AI stats + snapshots always run
- [ ] SmartLifeWebhookBridge is a single global instance — start when any entry enables it, stop when none do; never register the same webhook ID twice
- [ ] Android build: always use JDK 21; JDK 26 incompatible with Gradle 8.7
- [ ] Tablet reboot required after granting notification access on LineageOS (service must rebind)
- [ ] Android manifest: `foregroundServiceType="specialUse"` + `FOREGROUND_SERVICE_SPECIAL_USE` permission required for targetSdk=35 — missing causes immediate crash + 30-min restart penalty
- [ ] Android manifest: `usesCleartextTraffic="true"` on `<application>` — required for HTTP to local HA IP
- [ ] Android multipart: NEVER use `DataOutputStream.writeBytes()` for user-visible text — use `out.write(s.toByteArray(Charsets.UTF_8))` to preserve accented characters
- [ ] After installing new APK: force-stop the old process (`adb shell am force-stop com.alex.tuyabridge`) so new `onListenerConnected()` fires

## DPS Event Codes
- initiative_message: base64-encoded JSON — Brasil, Wallis, Germany cameras (newer firmware)
  Decoded v3.x: {"bucket":"ty-eu-storage30","files":[["/path.jpeg","key"]]}
  Decoded v4.0: {"v":"4.0","cmd":"ipc_motion","files":[{"data":"hex","keyId":"default","iv":"hex"}]}
  v3.x: OSS download works. v4.0: CANNOT decrypt — keyId="default" key unknown, not exposed via API.
  30+ candidates tried (local_key, mqtt_key, access_secret, product_id, uuid, all combos) — none work.
- movement_detect_pic: base64-encoded JSON (newer fw) or raw JSON (older) — Winti cameras AND Brasil cameras
  ALWAYS decode with base64.b64decode() first; if that fails, try json.loads() directly.
  Winti clean format (some cameras): {"bucket":"ty-eu-storage30-pic","files":[["/path.jpeg","filekey"]]}
    → OSS download + AES-ECB/CBC decryption works (clean path, non-empty key)
  ?param= format (Brasil cameras AND Winti Camera Door — confirmed 2026-06-04):
    {"bucket":"ty-eu-storage30-pic","files":[["/path.jpeg?param=BASE64SIG",""]]}
    → ?param= is a CDN auth token. file_key is always empty.
    → v0.5.7: attempt direct HTTP GET first — plain JPEG returned by some CDN regions (EU/CH untested).
    → If fetch fails (403 or non-JPEG) → fall through to multi-snapshot.
    → IP-restriction confirmed for Brazil CDN. Camera Door result TBD (watch logs for "signed CDN fetch ok").
  OSS CDN URL format: images.tuyaeu.com/{bucket}{file_path} (bucket IS required in the path)
  WARNING: Do NOT assume all Winti cameras use clean format — Camera Door uses ?param= despite being CH.

## OSS Image Decryption
- OSS images use AES-ECB (older cameras) OR 4-byte header + 16-byte IV + 44-byte pad + AES-CBC (newer)
- Both tried automatically in camera_api._decrypt_image
- file_key from files[0][1]: hex string → bytes.fromhex(); short ASCII → pad to 16 bytes
- Use `cryptography` lib only (NOT pycryptodome)

## MQTT Decrypt Reference
- Protocol: AES-128-ECB
- Key: mqtt_password[8:24].encode()  (16 bytes, session-specific)
- Endpoint: POST /v1.0/open-hub/access/config
- Broker: ssl://m1.tuyaeu.com:8883
- Motion DPS codes: initiative_message, movement_detect_pic
