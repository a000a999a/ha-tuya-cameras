"""Format SD card button — one per camera."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .camera_api import CameraAPI
from .const import DOMAIN
from .coordinator import CameraCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data        = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    camera_api  = data["camera_api"]
    cameras     = (coordinator.data or {}).get("cameras", {})

    async_add_entities([
        FormatSDButton(entry, dev_id, cam, camera_api)
        for dev_id, cam in cameras.items()
    ])


class FormatSDButton(ButtonEntity):
    """Triggers an SD card format on the camera via Tuya API."""

    _attr_has_entity_name = True
    _attr_name            = "Format SD Card"
    _attr_icon            = "mdi:sd"

    def __init__(
        self,
        entry: ConfigEntry,
        dev_id: str,
        cam: dict,
        camera_api: CameraAPI,
    ) -> None:
        self._dev_id         = dev_id
        self._camera_api     = camera_api
        self._attr_unique_id = f"{entry.entry_id}_{dev_id}_format_sd"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, dev_id)},
            name=cam["name"],
            manufacturer="Tuya",
            model="IP Camera",
            suggested_area=cam.get("area"),
        )

    def press(self) -> None:
        ok = self._camera_api.format_sd(self._dev_id)
        if not ok:
            _LOGGER.error("SD format failed for %s", self._dev_id)
        else:
            _LOGGER.info("SD format triggered for %s", self._dev_id)
