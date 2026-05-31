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
SD_THRESHOLD_DEFAULT = 90  # percent

# Coordinator
COORDINATOR_UPDATE_INTERVAL_MINUTES = 15

# SMTP defaults
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 587
