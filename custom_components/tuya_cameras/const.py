"""Constants for Tuya Cameras."""

DOMAIN      = "tuya_cameras"
DOMAIN_CORE = "tuya_home_core"

# Config / data keys
CONF_CORE_ENTRY_ID   = "core_entry_id"
CONF_SMTP_HOST       = "smtp_host"
CONF_SMTP_PORT       = "smtp_port"
CONF_SMTP_SENDER     = "smtp_sender"
CONF_SMTP_PASSWORD   = "smtp_password"
CONF_RECIPIENTS      = "recipients"   # stored in options
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

# SMTP defaults
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 587

# AI detection
CONF_AI_ENABLED = "ai_detection_enabled"
CONF_AI_URL     = "ai_detection_url"
DEFAULT_AI_URL  = "http://localhost:8000"
EVENT_AI_UPDATED = f"{DOMAIN}_ai_updated"
