"""Tuya Cameras — SD monitoring and real-time motion alerts."""

from __future__ import annotations

import logging
import os

import yaml

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er

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


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register domain-wide services (called once regardless of how many entries exist)."""

    async def _handle_refresh_all(call: ServiceCall) -> None:
        """Refresh every tuya_cameras entry, then regenerate the Lovelace dashboard."""
        entries = hass.data.get(DOMAIN, {})
        if not entries:
            _LOGGER.warning("refresh_all: no tuya_cameras entries loaded")
            return
        for entry_id, data in entries.items():
            core_coord = data.get("core_coord")
            cam_coord  = data.get("coordinator")
            try:
                if core_coord:
                    await core_coord.async_refresh()
                if cam_coord:
                    await cam_coord.async_refresh()
                _LOGGER.debug("refresh_all: refreshed entry %s", entry_id)
            except Exception as err:
                _LOGGER.error("refresh_all: error refreshing entry %s: %s", entry_id, err)
        _LOGGER.info("refresh_all: done (%d entries refreshed)", len(entries))
        await _generate_lovelace_dashboard(hass)

    hass.services.async_register(DOMAIN, "refresh_all", _handle_refresh_all)
    return True


async def _generate_lovelace_dashboard(hass: HomeAssistant) -> None:
    """
    Write tuya_cameras_lovelace.yaml to the HA config directory.

    Groups all cameras by area across every tuya_cameras entry.
    Each area becomes a separate dashboard view (tab).
    Entity IDs are resolved via the entity registry — cameras not yet registered
    (newly discovered, requiring an integration reload) are skipped with a log warning.
    """
    registry   = er.async_get(hass)
    domain_data = hass.data.get(DOMAIN, {})

    areas: dict[str, list[dict]] = {}
    missing: list[str] = []

    for entry_id, data in domain_data.items():
        cam_data = (data.get("coordinator").data or {}).get("cameras", {})
        for dev_id, cam in cam_data.items():
            area = cam.get("area") or "Unknown"

            sd_eid     = registry.async_get_entity_id("sensor", DOMAIN, f"{entry_id}_{dev_id}_sd_pct")
            online_eid = registry.async_get_entity_id("sensor", DOMAIN, f"{entry_id}_{dev_id}_online")
            fmt_eid    = registry.async_get_entity_id("button", DOMAIN, f"{entry_id}_{dev_id}_format_sd")

            if not online_eid and not sd_eid:
                missing.append(cam.get("name", dev_id))
                continue

            areas.setdefault(area, []).append({
                "name":       cam["name"],
                "online":     cam.get("online", False),
                "online_eid": online_eid,
                "sd_eid":     sd_eid,
                "fmt_eid":    fmt_eid,
            })

    if missing:
        _LOGGER.warning(
            "Lovelace gen: %d camera(s) have no registered entities yet (integration reload needed): %s",
            len(missing), ", ".join(missing),
        )

    if not areas:
        _LOGGER.warning("Lovelace gen: no camera entities found — skipping YAML write")
        return

    views = []
    for area in sorted(areas):
        cams  = sorted(areas[area], key=lambda c: c["name"])
        cards = []

        for cam in cams:
            entities = []
            if cam["online_eid"]:
                entities.append({"entity": cam["online_eid"], "name": "Status"})
            if cam["sd_eid"]:
                entities.append({"entity": cam["sd_eid"],     "name": "SD Card"})
            if cam["fmt_eid"]:
                entities.append({"entity": cam["fmt_eid"],    "name": "Format SD Card"})

            if entities:
                cards.append({
                    "type":     "entities",
                    "title":    cam["name"],
                    "icon":     "mdi:cctv" if cam["online"] else "mdi:cctv-off",
                    "entities": entities,
                })

        if cards:
            views.append({
                "title":  f"{area}  ({len(cams)})",
                "path":   area.lower().replace(" ", "_"),
                "icon":   "mdi:cctv",
                "badges": [],
                "cards":  cards,
            })

    dashboard = {"title": "Tuya Cameras", "views": views}
    path = os.path.join(hass.config.config_dir, "tuya_cameras_lovelace.yaml")

    def _write() -> None:
        with open(path, "w", encoding="utf-8") as fh:
            yaml.dump(dashboard, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)

    await hass.async_add_executor_job(_write)
    total = sum(len(c) for c in areas.values())
    _LOGGER.info(
        "Lovelace dashboard written: %s  (%d areas · %d cameras)",
        path, len(areas), total,
    )


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
        core_coord     = core_coord,
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
