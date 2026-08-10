"""Config and options flow for Smart Home Solutions Energy."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ShsApiClient, ShsApiError, ShsPairingError
from .const import (
    CONFIGURABLE_CATEGORIES,
    CONF_BASE_URL,
    CONF_CUSTOMER_NAME,
    CONF_DEVICE_NAME,
    CONF_DEVICE_TOKEN,
    CONF_DEVICE_TOKEN_ID,
    CONF_HOME_ID,
    CONF_PAIRING_CODE,
    DEFAULT_BASE_URL,
    DOMAIN,
    OPT_PREFIX_ENTITIES,
    OPT_FORECAST_RESOLUTION_MINUTES,
    DEFAULT_FORECAST_RESOLUTION_MINUTES,
    OPT_PV_FORECAST_ENTITIES,
    OPT_SUPPLIER_IMPORT_FORECAST_ENTITY,
    OPT_SUPPLIER_EXPORT_FORECAST_ENTITY,
    OPT_ELECTRICITY_PRICE_AREA,
    OPT_PV_FORECAST_LATITUDE,
    OPT_PV_FORECAST_LONGITUDE,
    OPT_BATTERY_SOC_ENTITY,
    OPT_GRID_EXPORT_POWER_ENTITY,
    OPT_BATTERY_CAPACITY_KWH,
    OPT_BATTERY_CHARGE_MAX_W,
    OPT_BATTERY_DISCHARGE_MAX_W,
    OPT_BATTERY_MIN_SOC,
    OPT_BATTERY_MAX_SOC,
    OPT_BATTERY_TARGET_SOC,
    OPT_BATTERY_TARGET_IS_HARD,
    OPT_BATTERY_CHARGE_EFFICIENCY,
    OPT_BATTERY_DISCHARGE_EFFICIENCY,
    OPT_GRID_IMPORT_LIMIT_W,
    OPT_GRID_EXPORT_LIMIT_W,
    OPT_TERMINAL_SOC_MIN,
    OPT_TERMINAL_ENERGY_VALUE,
    OPT_POOL_POWER_W,
    OPT_POOL_ENABLED_ENTITY,
    OPT_POOL_MIN_RUN_SLOTS,
    OPT_POOL_DEADLINE,
    OPT_POOL_BASELINE_START,
    OPT_BOILER_POWER_W,
    OPT_BOILER_MIN_RUN_SLOTS,
    OPT_BOILER_DEADLINE,
    OPT_BOILER_BASELINE_START,
    OPT_EV_CONNECTED_ENTITY,
    OPT_EV_SOC_ENTITY,
    OPT_EV_TARGET_SOC_ENTITY,
    OPT_EV_DEPARTURE_ENTITY,
    OPT_EV_POWER_W,
    OPT_EV_BATTERY_KWH,
    OPT_EV_CHARGE_EFFICIENCY,
    OPT_EV_MIN_RUN_SLOTS,
    OPT_SUPPLIER_EXPORT_PRICE,
    OPT_SUPPLIER_IMPORT_PRICE,
)


class ShsEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pair with the SHS portal using a single-use pairing code."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            base_url = user_input[CONF_BASE_URL].rstrip("/")
            client = ShsApiClient(async_get_clientsession(self.hass), base_url)
            try:
                result = await client.pair(
                    user_input[CONF_PAIRING_CODE].strip().upper(),
                    user_input[CONF_DEVICE_NAME].strip() or "Home Assistant",
                )
            except ShsPairingError:
                errors["base"] = "invalid_pairing_code"
            except ShsApiError:
                errors["base"] = "cannot_connect"
            else:
                token_id = result["device_token_id"]
                await self.async_set_unique_id(token_id)
                self._abort_if_unique_id_configured()
                customer_name = result.get("customer_name") or "Smart Home Solutions"
                return self.async_create_entry(
                    title=customer_name,
                    data={
                        CONF_BASE_URL: base_url,
                        CONF_DEVICE_TOKEN: result["device_token"],
                        CONF_DEVICE_TOKEN_ID: token_id,
                        CONF_HOME_ID: result["home_id"],
                        CONF_CUSTOMER_NAME: customer_name,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PAIRING_CODE): str,
                    vol.Required(CONF_DEVICE_NAME, default="Home Assistant"): str,
                    vol.Required(CONF_BASE_URL, default=DEFAULT_BASE_URL): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ShsEnergyOptionsFlow()


class ShsEnergyOptionsFlow(OptionsFlow):
    """Map energiprestanda categories to energy sensors."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        schema: dict[Any, Any] = {}
        for category in CONFIGURABLE_CATEGORIES:
            key = f"{OPT_PREFIX_ENTITIES}{category}"
            schema[
                vol.Optional(
                    key, default=self.config_entry.options.get(key, [])
                )
            ] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor",
                    device_class="energy",
                    multiple=True,
                )
            )

        # Left unset, the all-in price sensors simply stay unavailable rather
        # than reporting the grid share as if it were the whole price.
        for key in (OPT_SUPPLIER_IMPORT_PRICE, OPT_SUPPLIER_EXPORT_PRICE):
            current = self.config_entry.options.get(key)
            field = (
                vol.Optional(key, default=current) if current else vol.Optional(key)
            )
            schema[field] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            )

        schema[vol.Required(
            OPT_FORECAST_RESOLUTION_MINUTES,
            default=self.config_entry.options.get(
                OPT_FORECAST_RESOLUTION_MINUTES,
                DEFAULT_FORECAST_RESOLUTION_MINUTES,
            ),
        )] = vol.All(vol.Coerce(int), vol.In([15]))

        def entity_field(
            key: str, *, domain: str | None = "sensor", multiple: bool = False
        ) -> None:
            current = self.config_entry.options.get(key)
            field = vol.Optional(key, default=current) if current else vol.Optional(key)
            config = (
                selector.EntitySelectorConfig(multiple=multiple)
                if domain is None
                else selector.EntitySelectorConfig(
                    domain=domain, multiple=multiple
                )
            )
            schema[field] = selector.EntitySelector(
                config
            )

        entity_field(OPT_PV_FORECAST_ENTITIES, multiple=True)
        entity_field(OPT_SUPPLIER_IMPORT_FORECAST_ENTITY)
        entity_field(OPT_SUPPLIER_EXPORT_FORECAST_ENTITY)
        entity_field(OPT_BATTERY_SOC_ENTITY)
        entity_field(OPT_GRID_EXPORT_POWER_ENTITY)
        entity_field(OPT_POOL_ENABLED_ENTITY, domain=None)
        entity_field(OPT_EV_CONNECTED_ENTITY, domain=None)
        entity_field(OPT_EV_SOC_ENTITY)
        entity_field(OPT_EV_TARGET_SOC_ENTITY, domain=None)
        entity_field(OPT_EV_DEPARTURE_ENTITY, domain=None)

        number_defaults = {
            OPT_BATTERY_CAPACITY_KWH: None,
            OPT_BATTERY_CHARGE_MAX_W: None,
            OPT_BATTERY_DISCHARGE_MAX_W: None,
            OPT_BATTERY_MIN_SOC: 0.05,
            OPT_BATTERY_MAX_SOC: 1.0,
            OPT_BATTERY_TARGET_SOC: 0.8,
            OPT_BATTERY_CHARGE_EFFICIENCY: 0.95,
            OPT_BATTERY_DISCHARGE_EFFICIENCY: 0.95,
            OPT_GRID_IMPORT_LIMIT_W: None,
            OPT_GRID_EXPORT_LIMIT_W: None,
            OPT_TERMINAL_SOC_MIN: 0.2,
            OPT_TERMINAL_ENERGY_VALUE: None,
            OPT_POOL_POWER_W: None,
            OPT_POOL_MIN_RUN_SLOTS: 4,
            OPT_BOILER_POWER_W: None,
            OPT_BOILER_MIN_RUN_SLOTS: 2,
            OPT_EV_POWER_W: None,
            OPT_EV_BATTERY_KWH: None,
            OPT_EV_CHARGE_EFFICIENCY: 0.92,
            OPT_EV_MIN_RUN_SLOTS: 2,
            OPT_PV_FORECAST_LATITUDE: self.hass.config.latitude,
            OPT_PV_FORECAST_LONGITUDE: self.hass.config.longitude,
        }
        for key, default in number_defaults.items():
            current = self.config_entry.options.get(key, default)
            field = vol.Optional(key, default=current) if current is not None else vol.Optional(key)
            schema[field] = vol.Coerce(float)

        schema[vol.Optional(
            OPT_BATTERY_TARGET_IS_HARD,
            default=self.config_entry.options.get(OPT_BATTERY_TARGET_IS_HARD, True),
        )] = bool

        for key, default in {
            OPT_POOL_DEADLINE: "20:00",
            OPT_POOL_BASELINE_START: "12:00",
            OPT_BOILER_DEADLINE: "22:00",
            OPT_BOILER_BASELINE_START: "06:00",
        }.items():
            schema[vol.Optional(key, default=self.config_entry.options.get(key, default))] = str
        price_area = self.config_entry.options.get(OPT_ELECTRICITY_PRICE_AREA)
        schema[
            vol.Optional(OPT_ELECTRICITY_PRICE_AREA, default=price_area)
            if price_area else vol.Optional(OPT_ELECTRICITY_PRICE_AREA)
        ] = str

        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
