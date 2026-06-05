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
            new     = cameras_after - cameras_before
            removed = cameras_before - cameras_after
            if new:
                new_cameras_detected = True
                _LOGGER.info(
                    "refresh_all: %d new camera(s) in entry %s — reloading to create entities: %s",
                    len(new), entry_id, new,
                )
                hass.async_create_task(hass.config_entries.async_reload(entry_id))
            if removed:
                registry = er.async_get(hass)
                for entity in list(registry.entities.values()):
                    if entity.config_entry_id != entry_id or entity.platform != DOMAIN:
                        continue
                    parts = entity.unique_id.split("_")
                    if len(parts) >= 2 and parts[1] in removed:
                        _LOGGER.info("refresh_all: removing stale entity %s (device removed from account)", entity.entity_id)
                        registry.async_remove(entity.entity_id)

        _LOGGER.info("refresh_all: done (%d entries refreshed)", len(entries))

        if new_cameras_detected:
            # Allow entity registration to settle after config entry reload
            await asyncio.sleep(5)

        await _update_lovelace_views(hass)

    hass.services.async_register(DOMAIN, "refresh_all", _handle_refresh_all)
    return True


async def _update_lovelace_views(hass: HomeAssistant) -> None:
    """
    Update both the Cameras view and the AI Detections view in the Overview dashboard.

    Cameras view: fills picture-glance card entities for cards with empty entities.
    AI Detections view: rebuilds Last Human Detection tiles for all known areas;
      adds missing Live Counts / 7-Day Summary stats for new tuya_cameras entries.
    """
    registry    = er.async_get(hass)
    domain_data = hass.data.get(DOMAIN, {})

    # Build: lowercase camera name → entity IDs (for Cameras view)
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

    try:
        lovelace_obj = hass.data.get("lovelace")
        if not lovelace_obj:
            _LOGGER.warning("Lovelace update: no lovelace data in hass.data")
            return

        _LOGGER.debug("Lovelace type=%s attrs=%s", type(lovelace_obj).__name__,
                      [a for a in dir(lovelace_obj) if not a.startswith("_")])

        # Newer HA: LovelaceManager with .dashboards dict
        # Older HA: direct dashboard object or dict
        if hasattr(lovelace_obj, "dashboards"):
            dashboard = lovelace_obj.dashboards.get("lovelace") or lovelace_obj.dashboards.get("")
        elif isinstance(lovelace_obj, dict):
            dashboard = lovelace_obj.get("dashboards", {}).get("lovelace")
        else:
            dashboard = None

        if not dashboard or not hasattr(dashboard, "async_load"):
            _LOGGER.warning("Lovelace update: Overview dashboard not accessible (type=%s)", type(lovelace_obj).__name__)
            return

        config_obj = await dashboard.async_load(force=True)
        _LOGGER.debug("Lovelace config_obj type=%s", type(config_obj).__name__)
        if isinstance(config_obj, dict):
            config = config_obj
        elif hasattr(config_obj, "config") and isinstance(getattr(config_obj, "config", None), dict):
            config = config_obj.config
        elif hasattr(config_obj, "data") and isinstance(getattr(config_obj, "data", None), dict):
            config = config_obj.data
        else:
            _LOGGER.warning("Lovelace: cannot extract raw config from %s — skipping", type(config_obj).__name__)
            return
        changed = False

        for view in config.get("views", []):
            path = view.get("path", "")
            if path == "cameras" and name_map:
                if _patch_empty_card_entities(view, name_map):
                    changed = True
            elif path == "ai-detections":
                if _patch_ai_detections_view(view, registry):
                    changed = True

        if changed:
            await dashboard.async_save(config)
            _LOGGER.info("Lovelace views updated (cameras: %d, ai-detections patched)", len(name_map))
        else:
            _LOGGER.debug("Lovelace update: no changes needed")

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


def _patch_ai_detections_view(view: dict, registry) -> bool:
    """
    Rebuild the AI Detections view sections dynamically:

    • Last Human Detection — one tile per sensor.tuya_cameras_last_human_* entity,
      sorted alphabetically by area name, covering all tuya_cameras entries.
    • Live Counts — adds tile cards for any tuya_cameras entries not yet represented
      (e.g. Wallis entry stats appear after the Main Home stats).
    • 7-Day Summary — same: adds statistic cards for missing entries.
    """
    changed = False
    sections = view.get("sections", [])

    # ── Last Human Detection ─────────────────────────────────────────────────
    last_human: list[tuple[str, str]] = []
    for entry in registry.entities.values():
        eid = entry.entity_id
        if not eid.startswith("sensor.tuya_cameras_last_human_"):
            continue
        raw_name = (entry.original_name or "").replace("Last Human — ", "").strip()
        if not raw_name:
            raw_name = eid[len("sensor.tuya_cameras_last_human_"):].replace("_", " ").title()
        last_human.append((raw_name, eid))
    last_human.sort(key=lambda x: x[0])

    new_lh_cards = [{"type": "heading", "heading": "Last Human Detection"}]
    for area_name, sensor_eid in last_human:
        new_lh_cards.append({
            "type": "tile", "entity": sensor_eid,
            "name": area_name, "icon": "mdi:account-clock",
        })

    for section in sections:
        cards = section.get("cards", [])
        if any(c.get("heading") == "Last Human Detection" for c in cards):
            if cards != new_lh_cards:
                section["cards"] = new_lh_cards
                changed = True
            break

    # ── Live Counts — add tiles for missing entries ───────────────────────────
    all_processed = sorted(
        e.entity_id for e in registry.entities.values()
        if e.entity_id.startswith("sensor.tuya_cameras_ai_processed_7d")
    )
    for section in sections:
        cards = section.get("cards", [])
        if not any(c.get("heading") == "Live Counts" for c in cards):
            continue
        existing = {c.get("entity") for c in cards}
        for p_eid in all_processed:
            if p_eid in existing:
                continue
            suffix = p_eid[len("sensor.tuya_cameras_ai_processed_7d"):]
            h_eid  = f"sensor.tuya_cameras_ai_human_detected_7d{suffix}"
            d_eid  = f"sensor.tuya_cameras_ai_discarded_7d{suffix}"
            label  = f" ({suffix.lstrip('_')})" if suffix else ""
            cards += [
                {"type": "tile", "entity": p_eid,
                 "name": f"Processed (7d){label}", "icon": "mdi:image-multiple"},
                {"type": "tile", "entity": h_eid,
                 "name": f"Human Detected (7d){label}", "icon": "mdi:account-check", "color": "red"},
                {"type": "tile", "entity": d_eid,
                 "name": f"Discarded (7d){label}", "icon": "mdi:account-off"},
            ]
            changed = True
        break

    # ── 7-Day Summary — add statistic cards for missing entries ───────────────
    for section in sections:
        cards = section.get("cards", [])
        if not any(c.get("heading") == "7-Day Summary" for c in cards):
            continue
        existing = {c.get("entity") for c in cards}
        for p_eid in all_processed:
            if p_eid in existing:
                continue
            suffix = p_eid[len("sensor.tuya_cameras_ai_processed_7d"):]
            h_eid  = f"sensor.tuya_cameras_ai_human_detected_7d{suffix}"
            d_eid  = f"sensor.tuya_cameras_ai_discarded_7d{suffix}"
            label  = f" ({suffix.lstrip('_')})" if suffix else ""
            period = {"calendar": {"period": "week"}}
            cards += [
                {"type": "statistic", "entity": p_eid,
                 "name": f"Processed{label}", "stat_type": "state", "period": period},
                {"type": "statistic", "entity": h_eid,
                 "name": f"Human Detected{label}", "stat_type": "state", "period": period},
                {"type": "statistic", "entity": d_eid,
                 "name": f"Discarded{label}", "stat_type": "state", "period": period},
            ]
            changed = True
        break

    if changed:
        _LOGGER.debug("Lovelace: AI Detections view patched (%d last-human areas)", len(last_human))
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

    async def _cleanup_stale_entities() -> None:
        """Remove entity registry entries for cameras no longer in coordinator data."""
        current_ids = set((coordinator.data or {}).get("cameras", {}).keys())
        registry = er.async_get(hass)
        for entity in list(registry.entities.values()):
            if entity.config_entry_id != entry.entry_id or entity.platform != DOMAIN:
                continue
            parts = entity.unique_id.split("_")
            if len(parts) < 2 or len(parts[1]) < 18:
                continue
            dev_id = parts[1]
            if dev_id not in current_ids:
                _LOGGER.info("Removing stale entity %s (device %s removed from account)", entity.entity_id, dev_id)
                registry.async_remove(entity.entity_id)

    entry.async_on_unload(
        coordinator.async_add_listener(lambda: hass.async_create_task(_cleanup_stale_entities()))
    )
    await _cleanup_stale_entities()

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
