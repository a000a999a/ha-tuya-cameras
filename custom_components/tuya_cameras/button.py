"""Button entities for Tuya Cameras.

- RefreshButton (1 per integration): refreshes core device list then camera SD status.
- FormatSDButton (1 per camera): formats the SD card on a specific camera.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .camera_api import CameraAPI
from .const import CONF_RECIPIENTS, CONF_TECH_RECIPIENTS, DOMAIN
from .coordinator import CameraCoordinator
from .notify_helper import send_email

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data        = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    core_coord  = data["core_coord"]
    camera_api  = data["camera_api"]
    cameras     = (coordinator.data or {}).get("cameras", {})

    entities: list[ButtonEntity] = [
        RefreshButton(entry, core_coord, coordinator),
        TestMailerButton(entry),
        *[
            FormatSDButton(entry, dev_id, cam, camera_api)
            for dev_id, cam in cameras.items()
        ],
    ]
    async_add_entities(entities)


class RefreshButton(ButtonEntity):
    """
    Per-entry refresh: refreshes the core device list then camera SD status for this entry.
    Hidden from the main UI (DIAGNOSTIC) — use tuya_cameras.refresh_all service instead.
    """

    _attr_has_entity_name  = True
    _attr_name             = "Refresh"
    _attr_icon             = "mdi:refresh"
    _attr_entity_category  = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        entry: ConfigEntry,
        core_coordinator: any,
        cam_coordinator: CameraCoordinator,
    ) -> None:
        self._core_coord     = core_coordinator
        self._cam_coord      = cam_coordinator
        self._attr_unique_id = f"{entry.entry_id}_refresh"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Tuya Cameras",
            manufacturer="Tuya",
        )

    async def async_press(self) -> None:
        _LOGGER.info("Manual refresh: pulling device list from Tuya Cloud...")
        await self._core_coord.async_refresh()
        _LOGGER.info("Manual refresh: updating camera SD status...")
        await self._cam_coord.async_refresh()
        _LOGGER.info("Manual refresh complete")


class TestMailerButton(ButtonEntity):
    """Sends a test email to the tech recipient of every configured area."""

    _attr_has_entity_name  = True
    _attr_name             = "Test Mailer"
    _attr_icon             = "mdi:email-check"
    _attr_entity_category  = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry    = entry
        self._attr_unique_id = f"{entry.entry_id}_test_mailer"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Tuya Cameras",
            manufacturer="Tuya",
        )

    async def async_press(self) -> None:
        from datetime import datetime
        now        = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        recipients = self._entry.options.get(CONF_RECIPIENTS, {})

        for area, addr_map in recipients.items():
            tech_addrs = addr_map.get(CONF_TECH_RECIPIENTS, [])
            if not tech_addrs:
                _LOGGER.warning("Test mailer: no tech recipients for area %s", area)
                continue

            subject = f"[TEST] Tuya Cameras — {area}"
            body = (
                "<html><body>"
                f"<h2>Test notification — {area}</h2>"
                f"<p>Sent: {now}</p>"
                f"<p>SMTP is working correctly for area <b>{area}</b>.</p>"
                "</body></html>"
            )
            await send_email(self.hass, subject, body, tech_addrs)
            _LOGGER.info("Test email sent to %s for area %s", tech_addrs, area)


class FormatSDButton(ButtonEntity):
    """Triggers an SD card format on one specific camera via Tuya API."""

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
