"""Camera sensors — SD usage, online status, and animal detection config."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .ai_stats import AIStats
from .const import (
    CONF_ANIMAL_CLASSES,
    CONF_ANIMAL_ENABLED,
    CONF_CAMERA_ANIMAL_CONFIG,
    DOMAIN,
    EVENT_AI_UPDATED,
)
from .coordinator import CameraCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data        = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    ai_stats    = data.get("ai_stats")
    cameras     = (coordinator.data or {}).get("cameras", {})

    entities: list[SensorEntity] = []
    for dev_id, cam in cameras.items():
        entities.append(CameraSDSensor(coordinator, entry, dev_id, cam))
        entities.append(CameraOnlineSensor(coordinator, entry, dev_id, cam))
        entities.append(CameraAnimalSensor(coordinator, entry, dev_id, cam))

    if ai_stats is not None:
        for stat in ("total", "human", "other"):
            entities.append(AIStatsSensor(entry, ai_stats, stat))
        recipients = entry.options.get("recipients", {})
        for area in recipients:
            entities.append(AILastHumanSensor(entry, ai_stats, area))

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


class CameraAnimalSensor(CoordinatorEntity[CameraCoordinator], SensorEntity):
    """Animal detection configuration for one camera — reads live from entry options."""

    _attr_has_entity_name  = True
    _attr_name             = "Animal Detection"
    _attr_icon             = "mdi:paw"
    _attr_entity_category  = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: CameraCoordinator,
        entry: ConfigEntry,
        dev_id: str,
        cam: dict,
    ) -> None:
        super().__init__(coordinator)
        self._dev_id           = dev_id
        self._entry            = entry
        self._attr_unique_id   = f"{entry.entry_id}_{dev_id}_animal"
        self._attr_device_info = _device_info(entry, cam)

    @property
    def native_value(self) -> str:
        cfg     = self._entry.options.get(CONF_CAMERA_ANIMAL_CONFIG, {}).get(self._dev_id, {})
        if not cfg.get(CONF_ANIMAL_ENABLED):
            return "off"
        classes = cfg.get(CONF_ANIMAL_CLASSES, [])
        return ", ".join(classes) if classes else "any animal"


_STAT_LABELS = {
    "total": ("AI Processed (7d)", "mdi:image-multiple"),
    "human": ("AI Human Detected (7d)", "mdi:account-check"),
    "other": ("AI Discarded (7d)", "mdi:account-off"),
}


class AIStatsSensor(SensorEntity):
    """Rolling 7-day count of AI analysed images: total / human / other."""

    _attr_has_entity_name = True
    _attr_state_class     = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "images"

    def __init__(self, entry: ConfigEntry, ai_stats: AIStats, stat: str) -> None:
        self._ai_stats = ai_stats
        self._stat     = stat
        label, icon    = _STAT_LABELS[stat]
        self._attr_name        = label
        self._attr_icon        = icon
        self._attr_unique_id   = f"{entry.entry_id}_ai_{stat}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Tuya Cameras",
            manufacturer="Tuya",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_AI_UPDATED, self._on_update)
        )

    async def _on_update(self, _event) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return self._ai_stats.counts()[self._stat]


class AILastHumanSensor(SensorEntity):
    """Timestamp of the last human detection for one area."""

    _attr_has_entity_name  = True
    _attr_device_class     = SensorDeviceClass.TIMESTAMP
    _attr_icon             = "mdi:account-clock"

    def __init__(self, entry: ConfigEntry, ai_stats: AIStats, area: str) -> None:
        self._ai_stats = ai_stats
        self._area     = area
        self._attr_name      = f"Last Human — {area}"
        self._attr_unique_id = f"{entry.entry_id}_ai_last_human_{area.lower().replace(' ', '_')}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Tuya Cameras",
            manufacturer="Tuya",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_AI_UPDATED, self._on_update)
        )

    async def _on_update(self, _event) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self):
        from datetime import datetime, timezone
        ts = self._ai_stats.last_human_ts(self._area)
        if ts is None:
            return None
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            return None
