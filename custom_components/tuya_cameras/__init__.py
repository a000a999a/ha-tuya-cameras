"""Tuya Cameras — SD monitoring and real-time motion alerts."""

from __future__ import annotations

import asyncio
import logging

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
        """
        Refresh every tuya_cameras entry then update the Lovelace Cameras view.

        If new cameras are discovered in any entry (not present before the refresh),
        that entry is reloaded so HA creates sensor/button entities for the new
        cameras. A short wait is inserted before the Lovelace update so the entity
        registry has time to settle after the reload.
        """
        entries = hass.data.get(DOMAIN, {})
        if not entries:
            _LOGGER.warning("refresh_all: no tuya_cameras entries loaded")
            return

        new_cameras_detected = False

        for entry_id, data in list(entries.items()):
            core_coord = data.get("core_coord")
            cam_coord  = data.get("coordinator")

            cameras_before = set((cam_coord.data or {}).get("cameras", {}).keys()) if cam_coord else set()

            try:
                if core_coord:
                    await core_coord.async_refresh()
                if cam_coord:
                    await cam_coord.async_refresh()
            except Exception as err:
                _LOGGER.error("refresh_all: error refreshing entry %s: %s", entry_id, err)
                continue

            cameras_after = set((cam_coord.data or {}).get("cameras", {}).keys()) if cam_coord else set()
            new = cameras_after - cameras_before
            if new:
                new_cameras_detected = True
                _LOGGER.info(
                    "refresh_all: %d new camera(s) in entry %s — reloading to create entities: %s",
                    len(new), entry_id, new,
                )
                hass.async_create_task(hass.config_entries.async_reload(entry_id))

        _LOGGER.info("refresh_all: done (%d entries refreshed)", len(entries))

        if new_cameras_detected:
            # Allow entity registration to settle after config entry reload
            await asyncio.sleep(5)

        await _update_lovelace_cameras_view(hass)

    hass.services.async_register(DOMAIN, "refresh_all", _handle_refresh_all)
    return True


async def _update_lovelace_cameras_view(hass: HomeAssistant) -> None:
    """
    Update picture-glance card entities in the Lovelace Cameras view.

    Matches cards by title (case-insensitive) against camera names from the
    coordinator. Only fills in entities for cards that currently have none —
    existing cards with entities are left untouched so manual customisations
    are preserved.
    """
    registry    = er.async_get(hass)
    domain_data = hass.data.get(DOMAIN, {})

    # Build: lowercase camera name → entity IDs
    name_map: dict[str, dict] = {}
    for entry_id, data in domain_data.items():
        cam_data = (data.get("coordinator").data or {}).get("cameras", {})
        for dev_id, cam in cam_data.items():
            name = cam.get("name", "").strip()
            if not name:
                continue
            sd_eid     = registry.async_get_entity_id("sensor", DOMAIN, f"{entry_id}_{dev_id}_sd_pct")
            online_eid = registry.async_get_entity_id("sensor", DOMAIN, f"{entry_id}_{dev_id}_online")
            fmt_eid    = registry.async_get_entity_id("button", DOMAIN, f"{entry_id}_{dev_id}_format_sd")
            if sd_eid or online_eid:
                name_map[name.lower()] = {
                    "sd": sd_eid, "online": online_eid, "fmt": fmt_eid,
                }

    if not name_map:
        _LOGGER.warning("Lovelace update: no camera entities in registry yet")
        return

    try:
        lovelace   = hass.data.get("lovelace", {})
        dashboard  = lovelace.get("dashboards", {}).get("lovelace")
        if not dashboard or not hasattr(dashboard, "async_load"):
            _LOGGER.warning("Lovelace update: Overview dashboard not accessible via API")
            return

        config  = await dashboard.async_load(force=True)
        changed = False

        for view in config.get("views", []):
            if view.get("path") == "cameras":
                changed = _patch_empty_card_entities(view, name_map)
                break

        if changed:
            await dashboard.async_save(config)
            _LOGGER.info("Lovelace Cameras view updated (%d cameras in registry)", len(name_map))
        else:
            _LOGGER.debug("Lovelace update: all cards already have entities — nothing changed")

    except Exception as err:
        _LOGGER.error("Lovelace update failed: %s", err)


def _patch_empty_card_entities(view: dict, name_map: dict) -> bool:
    """
    Walk sections in the Cameras view. For each picture-glance card whose
    entities list is empty, look up the camera by card title and fill in the
    SD usage, status, and Format SD Card entity IDs.

    Cards that already have entities are never modified, so manual layouts
    for Brasil / Winterthur / Farm are preserved.
    """
    changed = False
    for section in view.get("sections", []):
        for card in section.get("cards", []):
            if card.get("type") != "picture-glance":
                continue
            if card.get("entities"):          # already populated — leave it
                continue
            title = card.get("title", "").lower()
            cam   = name_map.get(title)
            if not cam:
                continue
            new_entities = [
                e for e in [
                    {"entity": cam["sd"]}     if cam["sd"]     else None,
                    {"entity": cam["online"]} if cam["online"] else None,
                    {"entity": cam["fmt"]}    if cam["fmt"]    else None,
                ] if e
            ]
            if new_entities:
                card["entities"] = new_entities
                _LOGGER.debug("Lovelace: filled entities for card '%s'", card.get("title"))
                changed = True
    return changed


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
