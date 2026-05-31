"""DataUpdateCoordinator for Tuya Cameras — polls SD status every 15 min."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .camera_api import CameraAPI
from .const import COORDINATOR_UPDATE_INTERVAL_MINUTES, DOMAIN

_LOGGER = logging.getLogger(__name__)


class CameraCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """
    Refreshes camera list and SD status on a 15-minute interval.
    Camera list sourced from tuya_home_core coordinator (no extra API call).
    SD status polled per camera from Tuya Cloud.

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
    ) -> None:
        self._camera_api      = camera_api
        self._core_coordinator = core_coordinator
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=COORDINATOR_UPDATE_INTERVAL_MINUTES),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        core_data = self._core_coordinator.data or {}
        devices   = core_data.get("devices", [])
        area_map  = core_data.get("areas", {})

        cameras = await self.hass.async_add_executor_job(
            self._camera_api.cameras_from_devices, devices, area_map
        )

        cam_map: dict[str, dict] = {}
        for cam in cameras:
            dev_id = cam["id"]
            try:
                sd = await self.hass.async_add_executor_job(
                    self._camera_api.get_sd_status, dev_id
                )
            except Exception as err:
                _LOGGER.warning("SD poll failed for %s: %s", dev_id, err)
                sd = {}
            cam_map[dev_id] = {**cam, **sd}

        if not cam_map and self.data:
            _LOGGER.warning("Camera refresh returned no cameras — keeping stale data")
            return self.data

        _LOGGER.debug("Camera coordinator refreshed: %d cameras", len(cam_map))
        return {"cameras": cam_map}
