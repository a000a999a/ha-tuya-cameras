"""Tuya Cameras — SD monitoring and real-time motion alerts."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .ai_client import AIClient
from .ai_stats import AIStats
from .camera_api import CameraAPI
from .const import (
    CONF_AI_ENABLED, CONF_AI_URL,
    CONF_CORE_ENTRY_ID, CONF_RECIPIENTS, CONF_REFRESH_DAYS, DEFAULT_REFRESH_DAYS,
    CONF_SMTP_HOST, CONF_SMTP_PASSWORD, CONF_SMTP_PORT, CONF_SMTP_SENDER,
    DOMAIN, DOMAIN_CORE,
)
from .coordinator import CameraCoordinator
from .mqtt_bridge import TuyaMQTTBridge
from .notify import Notifier

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor", "button"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    core_entry_id = entry.data[CONF_CORE_ENTRY_ID]

    if DOMAIN_CORE not in hass.data or core_entry_id not in hass.data[DOMAIN_CORE]:
        raise ConfigEntryNotReady("Tuya Home Core is not loaded yet.")

    core        = hass.data[DOMAIN_CORE][core_entry_id]
    tuya_client = core["api"].client
    core_coord  = core["coordinator"]

    smtp_config = {
        CONF_SMTP_HOST:     entry.data[CONF_SMTP_HOST],
        CONF_SMTP_PORT:     entry.data[CONF_SMTP_PORT],
        CONF_SMTP_SENDER:   entry.data[CONF_SMTP_SENDER],
        CONF_SMTP_PASSWORD: entry.data[CONF_SMTP_PASSWORD],
    }
    notifier     = Notifier(smtp_config)
    camera_api   = CameraAPI(tuya_client)
    refresh_days = entry.options.get(CONF_REFRESH_DAYS, DEFAULT_REFRESH_DAYS)
    coordinator  = CameraCoordinator(hass, camera_api, core_coord, refresh_days)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        raise ConfigEntryNotReady(f"Camera status unavailable: {err}") from err

    recipients = entry.options.get(CONF_RECIPIENTS, {})
    uid        = core.get("uid", "")
    access_id  = core["api"]._api_key

    if not uid:
        _LOGGER.warning(
            "Tuya UID not set — MQTT bridge disabled. "
            "Go to Tuya Home Core → Configure and save to trigger auto-detection."
        )

    ai_enabled = entry.options.get(CONF_AI_ENABLED, False)
    ai_url     = entry.options.get(CONF_AI_URL, "")
    ai_client  = AIClient(ai_url) if ai_enabled and ai_url else None
    ai_stats   = AIStats(hass, entry.entry_id)
    await ai_stats.async_load()

    if ai_client:
        _LOGGER.info("AI detection enabled, service: %s", ai_url)

    bridge = TuyaMQTTBridge(
        hass           = hass,
        tuya_client    = tuya_client,
        camera_api     = camera_api,
        notifier       = notifier,
        recipients_cfg = recipients,
        uid            = uid,
        access_id      = access_id,
        ai_client      = ai_client,
        ai_stats       = ai_stats,
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator":  coordinator,
        "core_coord":   core_coord,
        "camera_api":   camera_api,
        "notifier":     notifier,
        "bridge":       bridge,
        "ai_stats":     ai_stats,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    bridge.start(hass)
    _LOGGER.info(
        "Tuya Cameras loaded: %d camera(s), refresh every %d day(s)",
        len((coordinator.data or {}).get("cameras", {})),
        refresh_days,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data[DOMAIN].get(entry.entry_id, {})
    bridge: TuyaMQTTBridge | None = data.get("bridge")
    if bridge:
        bridge.stop()

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
