# CLAUDE.md — ha-tuya-cameras

Extends /home/alex/ha-projects/CLAUDE.md. Read that file first.

## This Repo's Role
Camera SD monitoring and real-time motion alerts via Tuya MQTT.
Depends on tuya_home_core for Tuya credentials and area map.
No YOLO/AI — motion triggers email on any motion event.
See docs/yolo_extension.md for AI detection extension guide.

## Checklist Additions
- [ ] NEVER call gw.status() anywhere in this repo
- [ ] camera_api.py must load from .cameras.json cache; only re-fetch on explicit refresh
- [ ] coordinator polls SD status every 15 min — do not reduce this interval
- [ ] mqtt_bridge.py must track expire_time and reconnect 10 min before expiry
- [ ] mqtt_bridge.py decrypt key = password[8:24] — this changes every session
- [ ] MQTT credentials fetched once on connect, not per-message
- [ ] sensor unique_id = config_entry.entry_id + device_id + sensor_type
- [ ] button unique_id = config_entry.entry_id + device_id + "format_sd"
- [ ] notify.py must never raise — catch all SMTP exceptions and log them
- [ ] Per-area recipients come from options entry only — never hardcoded
- [ ] OSS image download attempted before HA snapshot fallback

## MQTT Decrypt Reference
- Protocol: AES-128-ECB
- Key: mqtt_password[8:24].encode()  (16 bytes, session-specific)
- Endpoint: POST /v1.0/open-hub/access/config
- Broker: ssl://m1.tuyaeu.com:8883
- Motion DPS codes: initiative_message, movement_detect_pic
