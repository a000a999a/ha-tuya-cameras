"""Config flow + options flow for Tuya Cameras."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CORE_ENTRY_ID,
    CONF_HUMAN_RECIPIENTS,
    CONF_RECIPIENTS,
    CONF_SMTP_HOST,
    CONF_SMTP_PASSWORD,
    CONF_SMTP_PORT,
    CONF_SMTP_SENDER,
    CONF_TECH_RECIPIENTS,
    DEFAULT_SMTP_HOST,
    DEFAULT_SMTP_PORT,
    DOMAIN,
    DOMAIN_CORE,
)


def _smtp_schema(defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema({
        vol.Required(CONF_SMTP_HOST,     default=d.get(CONF_SMTP_HOST, DEFAULT_SMTP_HOST)): str,
        vol.Required(CONF_SMTP_PORT,     default=d.get(CONF_SMTP_PORT, DEFAULT_SMTP_PORT)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=1, max=65535, mode=selector.NumberSelectorMode.BOX
            )),
        vol.Required(CONF_SMTP_SENDER,   default=d.get(CONF_SMTP_SENDER, "")): str,
        vol.Required(CONF_SMTP_PASSWORD, default=d.get(CONF_SMTP_PASSWORD, "")):
            selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
    })


def _area_schema(area: str, defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema({
        vol.Optional(CONF_HUMAN_RECIPIENTS, default=d.get(CONF_HUMAN_RECIPIENTS, "")):
            selector.TextSelector(selector.TextSelectorConfig(
                multiline=False,
                type=selector.TextSelectorType.EMAIL,
            )),
        vol.Optional(CONF_TECH_RECIPIENTS, default=d.get(CONF_TECH_RECIPIENTS, "")):
            selector.TextSelector(selector.TextSelectorConfig(
                multiline=False,
                type=selector.TextSelectorType.EMAIL,
            )),
    })


class TuyaCamerasConfigFlow(ConfigFlow, domain=DOMAIN):
    """Three-phase flow: pick core → SMTP → per-area recipients."""

    VERSION = 1

    def __init__(self) -> None:
        self._core_entry_id = ""
        self._smtp_data: dict = {}
        self._areas: list[str] = []
        self._recipients: dict = {}
        self._area_index = 0

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        core_entries = self.hass.config_entries.async_entries(DOMAIN_CORE)
        if not core_entries:
            return self.async_abort(reason="no_core_entry")

        if len(core_entries) == 1:
            self._core_entry_id = core_entries[0].entry_id
            return await self.async_step_smtp()

        if user_input is not None:
            self._core_entry_id = user_input[CONF_CORE_ENTRY_ID]
            return await self.async_step_smtp()

        schema = vol.Schema({
            vol.Required(CONF_CORE_ENTRY_ID): selector.SelectSelector(
                selector.SelectSelectorConfig(options=[
                    selector.SelectOptionDict(value=e.entry_id, label=e.title)
                    for e in core_entries
                ])
            )
        })
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_smtp(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._smtp_data = {
                **user_input,
                CONF_SMTP_PORT: int(user_input[CONF_SMTP_PORT]),
            }
            return await self._load_areas_and_next()

        return self.async_show_form(
            step_id="smtp", data_schema=_smtp_schema(), errors=errors
        )

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
                CONF_HUMAN_RECIPIENTS: user_input.get(CONF_HUMAN_RECIPIENTS, ""),
                CONF_TECH_RECIPIENTS:  user_input.get(CONF_TECH_RECIPIENTS, ""),
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
        return self.async_create_entry(
            title="Tuya Cameras",
            data={
                CONF_CORE_ENTRY_ID: self._core_entry_id,
                **self._smtp_data,
            },
            options={CONF_RECIPIENTS: self._recipients},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TuyaCamerasOptionsFlow(config_entry)


class TuyaCamerasOptionsFlow(OptionsFlow):
    """Options: update SMTP or edit per-area recipients."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry        = config_entry
        self._recipients   = dict(config_entry.options.get(CONF_RECIPIENTS, {}))
        self._selected_area: str = ""

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["edit_smtp", "edit_recipients"],
        )

    async def async_step_edit_smtp(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            new_data = {**self._entry.data, **user_input, CONF_SMTP_PORT: int(user_input[CONF_SMTP_PORT])}
            self.hass.config_entries.async_update_entry(self._entry, data=new_data)
            return self.async_create_entry(data={CONF_RECIPIENTS: self._recipients})

        defaults = {k: self._entry.data.get(k) for k in (
            CONF_SMTP_HOST, CONF_SMTP_PORT, CONF_SMTP_SENDER, CONF_SMTP_PASSWORD
        )}
        return self.async_show_form(
            step_id="edit_smtp", data_schema=_smtp_schema(defaults)
        )

    async def async_step_edit_recipients(self, user_input: dict | None = None) -> ConfigFlowResult:
        areas = list(self._recipients.keys())
        core = self.hass.data.get(DOMAIN_CORE, {}).get(
            self._entry.data.get(CONF_CORE_ENTRY_ID), {}
        )
        coord = core.get("coordinator")
        if coord and coord.data:
            live_areas = sorted({a for a in coord.data.get("areas", {}).values() if a})
            for a in live_areas:
                if a not in self._recipients:
                    self._recipients[a] = {CONF_HUMAN_RECIPIENTS: "", CONF_TECH_RECIPIENTS: ""}
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
                CONF_HUMAN_RECIPIENTS: user_input.get(CONF_HUMAN_RECIPIENTS, ""),
                CONF_TECH_RECIPIENTS:  user_input.get(CONF_TECH_RECIPIENTS, ""),
            }
            return self.async_create_entry(data={CONF_RECIPIENTS: self._recipients})

        return self.async_show_form(
            step_id="edit_area",
            data_schema=_area_schema(area, self._recipients.get(area, {})),
            description_placeholders={"area": area},
        )
