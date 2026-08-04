"""DataUpdateCoordinator for Tuya Cameras."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .camera_api import CameraAPI
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _parse_sd_storge(status: dict) -> dict:
    """Parse Tuya 'sd_storge' value 'total_kb|used_kb|free_kb' into coordinator SD fields."""
    raw = status.get("sd_storge", "")
    sd_status_code = status.get("sd_status")
    if not isinstance(raw, str) or "|" not in raw:
        return {}
    try:
        total_kb, used_kb, free_kb = (int(x) for x in raw.split("|"))
    except (ValueError, IndexError):
        return {}
    if total_kb == 0:
        return {"sd_status": "no_card" if sd_status_code != 1 else "unknown"}
    return {
        "sd_pct":      round(used_kb / total_kb * 100, 1),
        "sd_used_gb":  round(used_kb  / 1_048_576, 1),
        "sd_total_gb": round(total_kb / 1_048_576, 1),
        "sd_free_gb":  round(free_kb  / 1_048_576, 1),
        "sd_status":   "normal" if sd_status_code == 1 else "error",
    }


class CameraCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """
    Refreshes camera list and SD status on a user-configurable interval (default 14 days).
    Camera list sourced from tuya_home_core coordinator (no extra API call).
    Falls back to HA entity registry when Tuya device API is unavailable.
    SD status polled per camera from Tuya Cloud (skipped when API unavailable).
    Manual refresh available via the central Refresh button entity.

    data layout:
      {
        "cameras": {
          device_id: {
            "id", "name", "area", "online",
            "sd_pct", "sd_used_gb", "sd_total_gb", "sd_free_gb", "sd_status"
          }
        }
      }
    """

    def __init__(
        self,
        hass: HomeAssistant,
        camera_api: CameraAPI,
        core_coordinator: Any,
        refresh_days: int,
    ) -> None:
        self._camera_api       = camera_api
        self._core_coordinator = core_coordinator
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(days=refresh_days),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        core_data    = self._core_coordinator.data or {}
        devices      = core_data.get("devices", [])
        area_map     = core_data.get("areas", {})
        hub_entry_id = core_data.get("hub_entry_id", "")

        cameras = await self.hass.async_add_executor_job(
            self._camera_api.cameras_from_devices, devices, area_map
        )

        if not cameras:
            cameras = self._cameras_from_registry(hub_entry_id)
            if cameras:
                _LOGGER.info(
                    "Tuya device API unavailable — %d cameras loaded from HA entity registry%s",
                    len(cameras),
                    "" if hub_entry_id else " (UNSCOPED — no linked Tuya hub configured in "
                                            "tuya_home_core; may include other projects' cameras)",
                )

        cam_map: dict[str, dict] = {}
        for cam in cameras:
            dev_id = cam["id"]
            sd: dict = {}
            if devices:
                try:
                    sd = await self.hass.async_add_executor_job(
                        self._camera_api.get_sd_status, dev_id
                    )
                except Exception as err:
                    _LOGGER.warning("SD poll failed for %s: %s", dev_id, err)
            cam_map[dev_id] = {**cam, **sd}

        # IoT API unavailable — fall back to tuya_sharing device_map for SD data
        if not devices:
            sd_data = await self._fetch_sd_from_tuya_sharing(set(cam_map.keys()))
            for dev_id, sd in sd_data.items():
                if dev_id in cam_map:
                    cam_map[dev_id].update(sd)
            if sd_data:
                with_pct = sum(1 for sd in sd_data.values() if "sd_pct" in sd)
                _LOGGER.info(
                    "SD data sourced from tuya_sharing for %d camera(s) (%d with usage data)",
                    len(sd_data), with_pct,
                )

        if not cam_map and self.data:
            _LOGGER.warning("Camera refresh returned no cameras — keeping stale data")
            return self.data

        _LOGGER.debug("Camera coordinator refreshed: %d cameras", len(cam_map))
        return {"cameras": cam_map}

    async def _fetch_sd_from_tuya_sharing(self, dev_ids: set[str]) -> dict[str, dict]:
        """Borrow the official Tuya integration's tuya_sharing manager to read SD status."""
        result: dict[str, dict] = {}
        try:
            for entry in self.hass.config_entries.async_entries("tuya"):
                runtime = getattr(entry, "runtime_data", None)
                manager = getattr(runtime, "manager", None)
                if not manager or not hasattr(manager, "device_map"):
                    continue
                for dev_id in dev_ids:
                    if dev_id in manager.device_map and dev_id not in result:
                        sd = _parse_sd_storge(manager.device_map[dev_id].status)
                        if sd:
                            result[dev_id] = sd
        except Exception as err:
            _LOGGER.debug("tuya_sharing SD fetch failed: %s", err)
        return result

    def _cameras_from_registry(self, hub_entry_id: str = "") -> list[dict]:
        """Build camera list from HA entity/device/area registry when Tuya API is unavailable.

        The official HA Tuya hub registers cameras with unique_id "tuya.{device_id}".
        Device names and area assignments come from the device + area registries.

        Scoped to `hub_entry_id` (the official "tuya" hub linked to this project in
        tuya_home_core) when set, so this project's cameras never mix with another
        project's. If unset, falls back to the old unscoped behaviour (logs a warning
        at the call site) — configure "Linked Tuya hub" in tuya_home_core to fix.
        """
        entity_reg = er.async_get(self.hass)
        device_reg = dr.async_get(self.hass)
        area_reg   = ar.async_get(self.hass)

        cameras = []
        for entity in entity_reg.entities.values():
            if not entity.entity_id.startswith("camera.") or entity.platform != "tuya":
                continue
            uid    = entity.unique_id or ""
            dev_id = uid.removeprefix("tuya.")
            if not dev_id or len(dev_id) < 10:
                continue

            name      = entity.name or entity.original_name or dev_id
            area_name = ""
            device    = device_reg.async_get(entity.device_id) if entity.device_id else None

            if hub_entry_id and (not device or hub_entry_id not in device.config_entries):
                continue

            if device:
                name = device.name_by_user or device.name or name
                if device.area_id:
                    area = area_reg.async_get_area(device.area_id)
                    if area:
                        area_name = area.name

            if not area_name:
                _LOGGER.warning(
                    "Camera '%s' (%s) has no area assigned in HA — motion alerts for it "
                    "will have no configured recipients and will be silently dropped. "
                    "Set an Area on its device entry (Settings > Devices & Services) to fix.",
                    name, dev_id,
                )

            cameras.append({"id": dev_id, "name": name, "area": area_name, "online": True})

        return cameras
