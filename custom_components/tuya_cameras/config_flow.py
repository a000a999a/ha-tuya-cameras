"""Config flow + options flow for Tuya Cameras."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .ai_client import AIClient
from .const import (
    ANIMAL_COCO_CLASSES,
    CONF_AI_ENABLED,
    CONF_AI_URL,
    CONF_ANIMAL_CLASSES,
    CONF_ANIMAL_ENABLED,
    CONF_CAMERA_ANIMAL_CONFIG,
    CONF_MQTT_ALERTS_ENABLED,
    CONF_WEBHOOK_ALERTS_ENABLED,
    WEBHOOK_ID,
    CONF_CORE_ENTRY_ID,
    CONF_HUMAN_RECIPIENTS,
    CONF_RECIPIENTS,
    CONF_REFRESH_DAYS,
    CONF_SD_ALERT_THRESHOLD,
    CONF_TECH_RECIPIENTS,
    CONF_RECORDING_ENABLED,
    CONF_RECORDING_DURATION_S,
    CONF_RECORDING_PATH,
    CONF_RECORDING_RETENTION_DAYS,
    DEFAULT_AI_URL,
    DEFAULT_REFRESH_DAYS,
    DEFAULT_SD_ALERT_THRESHOLD,
    DEFAULT_RECORDING_DURATION_S,
    DEFAULT_RECORDING_PATH,
    DEFAULT_RECORDING_RETENTION_DAYS,
    DOMAIN,
    DOMAIN_CORE,
)


# Notify-target selectors, scoped to the SMTP integration specifically — the
# email send path uses smtp.send_message (for HTML + inline-image support),
# which only accepts SMTP-backed notify entities as its target. `multiple`
# preserves the old comma/semicolon-separated multi-recipient-per-field
# behaviour, just as a proper multi-select instead of a parsed string.
def _area_schema(area: str, defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    notify_selector = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="notify", integration="smtp", multiple=True)
    )
    return vol.Schema({
        vol.Optional(CONF_HUMAN_RECIPIENTS, default=d.get(CONF_HUMAN_RECIPIENTS, [])): notify_selector,
        vol.Optional(CONF_TECH_RECIPIENTS,  default=d.get(CONF_TECH_RECIPIENTS, [])):  notify_selector,
    })


class TuyaCamerasConfigFlow(ConfigFlow, domain=DOMAIN):
    """Two-phase flow: pick core → per-area recipients."""

    VERSION = 1

    def __init__(self) -> None:
        self._core_entry_id = ""
        self._areas: list[str] = []
        self._recipients: dict = {}
        self._area_index = 0

    def _core_entry_label(self, entry) -> str:
        """Build a descriptive label: title + areas + device count from coordinator."""
        core_data = self.hass.data.get(DOMAIN_CORE, {}).get(entry.entry_id, {})
        coord = core_data.get("coordinator")
        if coord and coord.data:
            areas = sorted({a for a in coord.data.get("areas", {}).values() if a})
            n = len(coord.data.get("devices", []))
            if areas:
                return f"{entry.title} · {', '.join(areas)} ({n} devices)"
        return entry.title

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        core_entries = self.hass.config_entries.async_entries(DOMAIN_CORE)
        if not core_entries:
            return self.async_abort(reason="no_core_entry")

        if len(core_entries) == 1:
            self._core_entry_id = core_entries[0].entry_id
            return await self._load_areas_and_next()

        if user_input is not None:
            self._core_entry_id = user_input[CONF_CORE_ENTRY_ID]
            return await self._load_areas_and_next()

        schema = vol.Schema({
            vol.Required(CONF_CORE_ENTRY_ID): selector.SelectSelector(
                selector.SelectSelectorConfig(options=[
                    selector.SelectOptionDict(value=e.entry_id, label=self._core_entry_label(e))
                    for e in core_entries
                ])
            )
        })
        return self.async_show_form(step_id="user", data_schema=schema)

    async def _load_areas_and_next(self) -> ConfigFlowResult:
        core = self.hass.data.get(DOMAIN_CORE, {}).get(self._core_entry_id, {})
        coord = core.get("coordinator")
        if coord and coord.data:
            area_map = coord.data.get("areas", {})
            self._areas = sorted({a for a in area_map.values() if a})
        if not self._areas:
            return self._create()
        self._area_index = 0
        return await self.async_step_area_recipients()

    async def async_step_area_recipients(self, user_input: dict | None = None) -> ConfigFlowResult:
        area = self._areas[self._area_index]
        if user_input is not None:
            self._recipients[area] = {
                CONF_HUMAN_RECIPIENTS: user_input.get(CONF_HUMAN_RECIPIENTS, []),
                CONF_TECH_RECIPIENTS:  user_input.get(CONF_TECH_RECIPIENTS, []),
            }
            self._area_index += 1
            if self._area_index < len(self._areas):
                return await self.async_step_area_recipients()
            return self._create()

        return self.async_show_form(
            step_id="area_recipients",
            data_schema=_area_schema(area),
            description_placeholders={"area": area},
        )

    def _create(self) -> ConfigFlowResult:
        core_entry = self.hass.config_entries.async_get_entry(self._core_entry_id)
        title = core_entry.title if core_entry else "Tuya Cameras"
        return self.async_create_entry(
            title=title,
            data={CONF_CORE_ENTRY_ID: self._core_entry_id},
            options={CONF_RECIPIENTS: self._recipients},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TuyaCamerasOptionsFlow(config_entry)


class TuyaCamerasOptionsFlow(OptionsFlow):
    """Options: edit per-area recipients and other settings."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry               = config_entry
        self._recipients          = dict(config_entry.options.get(CONF_RECIPIENTS, {}))
        self._selected_area:      str = ""
        self._selected_device_id: str = ""
        self._animal_camera_name: str = ""

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "edit_recipients", "edit_settings", "edit_ai", "edit_alerts",
                "edit_animal", "edit_recording",
            ],
        )

    async def async_step_edit_recording(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            new_options = {
                **self._entry.options,
                CONF_RECIPIENTS:                 self._recipients,
                CONF_RECORDING_ENABLED:          bool(user_input[CONF_RECORDING_ENABLED]),
                CONF_RECORDING_DURATION_S:       int(user_input[CONF_RECORDING_DURATION_S]),
                CONF_RECORDING_PATH:             user_input[CONF_RECORDING_PATH].strip("/"),
                CONF_RECORDING_RETENTION_DAYS:   int(user_input[CONF_RECORDING_RETENTION_DAYS]),
            }
            return self.async_create_entry(data=new_options)

        o = self._entry.options
        schema = vol.Schema({
            vol.Optional(CONF_RECORDING_ENABLED, default=o.get(CONF_RECORDING_ENABLED, False)):
                selector.BooleanSelector(),
            vol.Optional(CONF_RECORDING_DURATION_S,
                         default=o.get(CONF_RECORDING_DURATION_S, DEFAULT_RECORDING_DURATION_S)):
                selector.NumberSelector(selector.NumberSelectorConfig(
                    min=10, max=300, step=5,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="seconds",
                )),
            vol.Optional(CONF_RECORDING_PATH,
                         default=o.get(CONF_RECORDING_PATH, DEFAULT_RECORDING_PATH)):
                selector.TextSelector(),
            vol.Optional(CONF_RECORDING_RETENTION_DAYS,
                         default=o.get(CONF_RECORDING_RETENTION_DAYS, DEFAULT_RECORDING_RETENTION_DAYS)):
                selector.NumberSelector(selector.NumberSelectorConfig(
                    min=1, max=90, step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="days",
                )),
        })
        return self.async_show_form(step_id="edit_recording", data_schema=schema)

    async def async_step_edit_settings(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            new_options = {
                **self._entry.options,
                CONF_RECIPIENTS:         self._recipients,
                CONF_REFRESH_DAYS:       int(user_input[CONF_REFRESH_DAYS]),
                CONF_SD_ALERT_THRESHOLD: int(user_input[CONF_SD_ALERT_THRESHOLD]),
            }
            return self.async_create_entry(data=new_options)

        current_refresh   = self._entry.options.get(CONF_REFRESH_DAYS, DEFAULT_REFRESH_DAYS)
        current_threshold = self._entry.options.get(CONF_SD_ALERT_THRESHOLD, DEFAULT_SD_ALERT_THRESHOLD)
        schema = vol.Schema({
            vol.Optional(CONF_REFRESH_DAYS, default=current_refresh):
                selector.NumberSelector(selector.NumberSelectorConfig(
                    min=1, max=30, step=1,
                    mode=selector.NumberSelectorMode.SLIDER,
                    unit_of_measurement="days",
                )),
            vol.Optional(CONF_SD_ALERT_THRESHOLD, default=current_threshold):
                selector.NumberSelector(selector.NumberSelectorConfig(
                    min=50, max=99, step=1,
                    mode=selector.NumberSelectorMode.SLIDER,
                    unit_of_measurement="%",
                )),
        })
        return self.async_show_form(step_id="edit_settings", data_schema=schema)

    async def async_step_edit_recipients(self, user_input: dict | None = None) -> ConfigFlowResult:
        areas = list(self._recipients.keys())
        cam_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        coord = cam_data.get("coordinator")
        if coord and coord.data:
            live_areas = sorted({cam["area"] for cam in coord.data.get("cameras", {}).values() if cam.get("area")})
            for a in live_areas:
                if a not in self._recipients:
                    self._recipients[a] = {CONF_HUMAN_RECIPIENTS: [], CONF_TECH_RECIPIENTS: []}
            areas = live_areas or areas

        if not areas:
            return self.async_abort(reason="no_areas")

        if user_input is not None:
            self._selected_area = user_input["area"]
            return await self.async_step_edit_area()

        schema = vol.Schema({
            vol.Required("area"): selector.SelectSelector(
                selector.SelectSelectorConfig(options=areas)
            )
        })
        return self.async_show_form(step_id="edit_recipients", data_schema=schema)

    async def async_step_edit_area(self, user_input: dict | None = None) -> ConfigFlowResult:
        area = self._selected_area
        if user_input is not None:
            self._recipients[area] = {
                CONF_HUMAN_RECIPIENTS: user_input.get(CONF_HUMAN_RECIPIENTS, []),
                CONF_TECH_RECIPIENTS:  user_input.get(CONF_TECH_RECIPIENTS, []),
            }
            return self.async_create_entry(data={**self._entry.options, CONF_RECIPIENTS: self._recipients})

        return self.async_show_form(
            step_id="edit_area",
            data_schema=_area_schema(area, self._recipients.get(area, {})),
            description_placeholders={"area": area},
        )

    async def async_step_edit_alerts(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            new_options = {
                **self._entry.options,
                CONF_RECIPIENTS:             self._recipients,
                CONF_MQTT_ALERTS_ENABLED:    user_input.get(CONF_MQTT_ALERTS_ENABLED, True),
                CONF_WEBHOOK_ALERTS_ENABLED: user_input.get(CONF_WEBHOOK_ALERTS_ENABLED, False),
            }
            return self.async_create_entry(data=new_options)

        mqtt_on    = self._entry.options.get(CONF_MQTT_ALERTS_ENABLED, True)
        webhook_on = self._entry.options.get(CONF_WEBHOOK_ALERTS_ENABLED, False)

        try:
            from homeassistant.helpers.network import get_url
            base = get_url(self.hass, prefer_internal=True)
        except Exception:
            base = "http://homeassistant.local:8123"
        webhook_url = f"{base.rstrip('/')}/api/webhook/{WEBHOOK_ID}"

        schema = vol.Schema({
            vol.Optional(CONF_MQTT_ALERTS_ENABLED, default=mqtt_on):
                selector.BooleanSelector(),
            vol.Optional(CONF_WEBHOOK_ALERTS_ENABLED, default=webhook_on):
                selector.BooleanSelector(),
        })
        return self.async_show_form(
            step_id="edit_alerts",
            data_schema=schema,
            description_placeholders={"webhook_url": webhook_url},
        )

    async def async_step_edit_ai(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            enabled = user_input.get(CONF_AI_ENABLED, False)
            url     = user_input.get(CONF_AI_URL, "").strip()
            if enabled and url:
                if not await AIClient(url).health_check():
                    errors[CONF_AI_URL] = "ai_unreachable"
            if not errors:
                new_options = {
                    **self._entry.options,
                    CONF_RECIPIENTS:  self._recipients,
                    CONF_AI_ENABLED:  enabled,
                    CONF_AI_URL:      url,
                }
                return self.async_create_entry(data=new_options)

        current_enabled = self._entry.options.get(CONF_AI_ENABLED, False)
        current_url     = self._entry.options.get(CONF_AI_URL, DEFAULT_AI_URL)
        schema = vol.Schema({
            vol.Optional(CONF_AI_ENABLED, default=current_enabled):
                selector.BooleanSelector(),
            vol.Optional(CONF_AI_URL, default=current_url): str,
        })
        return self.async_show_form(
            step_id="edit_ai", data_schema=schema, errors=errors
        )

    async def async_step_edit_animal(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Pick which camera to configure for animal detection."""
        cam_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        coord    = cam_data.get("coordinator")
        cameras: dict[str, str] = {}  # device_id → display name
        if coord and coord.data:
            for dev_id, cam in coord.data.get("cameras", {}).items():
                cameras[dev_id] = cam.get("name", dev_id)

        if not cameras:
            return self.async_abort(reason="no_cameras")

        if user_input is not None:
            self._selected_device_id = user_input["device_id"]
            self._animal_camera_name = cameras.get(self._selected_device_id, self._selected_device_id)
            return await self.async_step_edit_animal_config()

        schema = vol.Schema({
            vol.Required("device_id"): selector.SelectSelector(
                selector.SelectSelectorConfig(options=[
                    selector.SelectOptionDict(value=dev_id, label=name)
                    for dev_id, name in sorted(cameras.items(), key=lambda x: x[1])
                ])
            )
        })
        return self.async_show_form(step_id="edit_animal", data_schema=schema)

    async def async_step_edit_animal_config(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Configure animal detection for the selected camera."""
        device_id   = self._selected_device_id
        current_cfg = self._entry.options.get(CONF_CAMERA_ANIMAL_CONFIG, {}).get(device_id, {})

        if user_input is not None:
            animal_cfg          = dict(self._entry.options.get(CONF_CAMERA_ANIMAL_CONFIG, {}))
            animal_cfg[device_id] = {
                CONF_ANIMAL_ENABLED: user_input.get(CONF_ANIMAL_ENABLED, False),
                CONF_ANIMAL_CLASSES: user_input.get(CONF_ANIMAL_CLASSES, []),
            }
            new_options = {
                **self._entry.options,
                CONF_RECIPIENTS:          self._recipients,
                CONF_CAMERA_ANIMAL_CONFIG: animal_cfg,
            }
            return self.async_create_entry(data=new_options)

        schema = vol.Schema({
            vol.Optional(CONF_ANIMAL_ENABLED, default=current_cfg.get(CONF_ANIMAL_ENABLED, False)):
                selector.BooleanSelector(),
            vol.Optional(CONF_ANIMAL_CLASSES, default=current_cfg.get(CONF_ANIMAL_CLASSES, [])):
                selector.SelectSelector(selector.SelectSelectorConfig(
                    options=ANIMAL_COCO_CLASSES,
                    multiple=True,
                )),
        })
        return self.async_show_form(
            step_id="edit_animal_config",
            data_schema=schema,
            description_placeholders={"camera": self._animal_camera_name},
        )
