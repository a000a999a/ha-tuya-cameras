"""Camera sensors — SD usage and online status."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import CameraCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CameraCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    cameras = (coordinator.data or {}).get("cameras", {})

    entities: list[SensorEntity] = []
    for dev_id, cam in cameras.items():
        entities.append(CameraSDSensor(coordinator, entry, dev_id, cam))
        entities.append(CameraOnlineSensor(coordinator, entry, dev_id, cam))
    async_add_entities(entities)


def _device_info(entry: ConfigEntry, cam: dict) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, cam["id"])},
        name=cam["name"],
        manufacturer="Tuya",
        model="IP Camera",
        suggested_area=cam.get("area"),
    )


class CameraSDSensor(CoordinatorEntity[CameraCoordinator], SensorEntity):
    """SD card usage percentage for one camera."""

    _attr_native_unit_of_measurement = "%"
    _attr_device_class                = SensorDeviceClass.BATTERY  # closest built-in for %
    _attr_state_class                 = SensorStateClass.MEASUREMENT
    _attr_has_entity_name             = True
    _attr_name                        = "SD Usage"
    _attr_icon                        = "mdi:sd"

    def __init__(
        self,
        coordinator: CameraCoordinator,
        entry: ConfigEntry,
        dev_id: str,
        cam: dict,
    ) -> None:
        super().__init__(coordinator)
        self._dev_id             = dev_id
        self._attr_unique_id     = f"{entry.entry_id}_{dev_id}_sd_pct"
        self._attr_device_info   = _device_info(entry, cam)

    @property
    def _cam(self) -> dict:
        return (self.coordinator.data or {}).get("cameras", {}).get(self._dev_id, {})

    @property
    def native_value(self) -> float | None:
        return self._cam.get("sd_pct")

    @property
    def extra_state_attributes(self) -> dict:
        c = self._cam
        return {
            "sd_used_gb":  c.get("sd_used_gb"),
            "sd_total_gb": c.get("sd_total_gb"),
            "sd_free_gb":  c.get("sd_free_gb"),
            "sd_status":   c.get("sd_status"),
        }


class CameraOnlineSensor(CoordinatorEntity[CameraCoordinator], SensorEntity):
    """Online/offline status for one camera."""

    _attr_has_entity_name = True
    _attr_name            = "Status"
    _attr_icon            = "mdi:cctv"

    def __init__(
        self,
        coordinator: CameraCoordinator,
        entry: ConfigEntry,
        dev_id: str,
        cam: dict,
    ) -> None:
        super().__init__(coordinator)
        self._dev_id           = dev_id
        self._attr_unique_id   = f"{entry.entry_id}_{dev_id}_online"
        self._attr_device_info = _device_info(entry, cam)

    @property
    def _cam(self) -> dict:
        return (self.coordinator.data or {}).get("cameras", {}).get(self._dev_id, {})

    @property
    def native_value(self) -> str:
        return "online" if self._cam.get("online") else "offline"

    @property
    def extra_state_attributes(self) -> dict:
        c = self._cam
        return {"area": c.get("area"), "device_id": self._dev_id}
