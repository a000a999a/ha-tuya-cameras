"""Constants for Tuya Cameras."""

DOMAIN      = "tuya_cameras"
DOMAIN_CORE = "tuya_home_core"

# Config / data keys
CONF_CORE_ENTRY_ID   = "core_entry_id"
CONF_RECIPIENTS      = "recipients"   # stored in options
# Each value is a list of `notify.*` entity IDs (from HA's SMTP integration),
# not raw email strings — see notify_helper.py.
CONF_HUMAN_RECIPIENTS = "human"
CONF_TECH_RECIPIENTS  = "tech"

# HA token/URL for live snapshot fallback
CONF_HA_TOKEN = "ha_token"
CONF_HA_URL   = "ha_url"

# Tuya camera device categories
CAMERA_CATEGORIES = {"sp", "ipc", "dh", "nvr", "sp-new"}

# Motion event DPS codes
MOTION_CODES = {"initiative_message", "movement_detect_pic"}

# SD card
SD_STATUS_LABELS = {
    "1": "normal",
    "2": "needs format",
    "3": "formatting",
    "4": "format failed",
    "5": "no card",
}
CONF_SD_ALERT_THRESHOLD = "sd_alert_threshold"
DEFAULT_SD_ALERT_THRESHOLD = 90  # percent — alert when SD usage exceeds this

# Coordinator refresh
CONF_REFRESH_DAYS    = "refresh_days"
DEFAULT_REFRESH_DAYS = 14

# AI detection
CONF_AI_ENABLED = "ai_detection_enabled"
CONF_AI_URL     = "ai_detection_url"
DEFAULT_AI_URL  = "http://localhost:8000"
EVENT_AI_UPDATED = f"{DOMAIN}_ai_updated"

# Motion alert source toggles
CONF_MQTT_ALERTS_ENABLED    = "mqtt_alerts_enabled"
CONF_WEBHOOK_ALERTS_ENABLED = "webhook_alerts_enabled"
WEBHOOK_ID                  = "smartlife_motion"

# Per-camera animal detection (stored in options as {device_id: {enabled, classes}})
CONF_CAMERA_ANIMAL_CONFIG = "camera_animal_config"
CONF_ANIMAL_ENABLED       = "enabled"
CONF_ANIMAL_CLASSES       = "classes"
ANIMAL_COCO_CLASSES = [
    "bird", "cat", "dog", "horse", "sheep",
    "cow", "elephant", "bear", "zebra", "giraffe",
]

# Local recording on human detection (2026-08-31). Applies to both the MQTT and
# ONVIF motion paths — recording starts only after a human is confirmed, never on
# raw/unfiltered motion, so a false positive never burns a clip. See recording_helper.py.
CONF_RECORDING_ENABLED        = "recording_enabled"
CONF_RECORDING_DURATION_S     = "recording_duration_s"
CONF_RECORDING_PATH           = "recording_path"
CONF_RECORDING_RETENTION_DAYS = "recording_retention_days"
DEFAULT_RECORDING_DURATION_S     = 60
DEFAULT_RECORDING_PATH           = "tuya_cameras/recordings"  # relative to /config/www — see recording_helper.py
DEFAULT_RECORDING_RETENTION_DAYS = 7
