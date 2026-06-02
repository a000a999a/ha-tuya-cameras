# CLAUDE.md — ha-tuya-cameras

Extends /home/alex/ha-projects/CLAUDE.md. Read that file first.

## This Repo's Role
Camera SD monitoring and real-time MQTT motion alerts with AI human detection.
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

## Motion Event Flow (MQTT path — the only motion path)
1. Camera pushes DPS event (initiative_message or movement_detect_pic) via Tuya MQTT
2. Bridge decrypts payload (AES-128-ECB, key=mqtt_password[8:24])
3. Parses {devId, status[{code, value, t}]} — both DPS codes embed bucket+files
4. Try OSS image download (images.tuyaeu.com/{bucket}/{path}) — encrypted with file_key
5. If OSS fails → HA snapshot fallback (async_get_image via entity registry lookup)
6. If no image at all → discard silently (no email, no YOLO call)
7. Pass image to AI service → human detected: email annotated image / no human: discard

## Image Decryption
- OSS images are AES-128-ECB encrypted (most Brasil/Wallis cameras)
- Use `cryptography` library (in requirements) — NOT pycryptodome (not installed in HA)
- file_key from DPS payload: hex string (32 chars → bytes.fromhex) or ASCII padded to 16 bytes

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
- [ ] OSS image download attempted before HA snapshot fallback
- [ ] Image decryption uses cryptography library, not Crypto/pycryptodome

## DPS Event Codes
- initiative_message: base64-encoded JSON — Brasil, Wallis, Germany cameras (newer firmware)
  Decoded v3.x: {"bucket":"ty-eu-storage30","files":[["/path.jpeg","key"]]}
  Decoded v4.0: {"v":"4.0","cmd":"ipc_motion","files":[{"data":"hex","keyId":"default","iv":"hex"}]}
  v3.x: OSS download works. v4.0: CANNOT decrypt — keyId="default" key unknown, not exposed via API.
  30+ candidates tried (local_key, mqtt_key, access_secret, product_id, uuid, all combos) — none work.
- movement_detect_pic: base64-encoded JSON (newer fw) or raw JSON (older) — Winti cameras AND Brasil cameras
  ALWAYS decode with base64.b64decode() first; if that fails, try json.loads() directly.
  Winti format: {"bucket":"ty-eu-storage30-pic","files":[["/path.jpeg","filekey"]]}
    → OSS download + AES-ECB/CBC decryption works (clean path, non-empty key)
  Brasil format: {"bucket":"ty-eu-storage30-pic","files":[["/path.jpeg?param=BASE64SIG",""]]}
    → ?param= is a CDN auth token (32-byte HMAC, IP-restricted). Returns HTTP 403 from any 3rd-party IP.
    → file_key is always empty → cannot decrypt even if download succeeded.
    → SKIP OSS when path contains ?param= and key is empty. Use HA snapshot fallback.
  OSS CDN URL format: images.tuyaeu.com/{bucket}{file_path} (bucket IS required in the path)

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
