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
from .configuration import (
    async_discover_options,
    optimisation_defaults,
    resolved_options,
)
from .const import (
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
    OPT_PV_FORECAST_ENTITIES,
    OPT_SUPPLIER_IMPORT_FORECAST_ENTITY,
    OPT_SUPPLIER_EXPORT_FORECAST_ENTITY,
    OPT_ELECTRICITY_PRICE_AREA,
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
    OPT_EV_CHARGE_CURRENT_ENTITY,
    OPT_EV_ENERGY_REMAINING_ENTITY,
    OPT_EV_PHASE_COUNT,
    OPT_EV_VOLTAGE,
    OPT_EV_DEFAULT_DEPARTURE,
    OPT_POOL_PLANNING_ENABLED,
    OPT_BOILER_PLANNING_ENABLED,
    OPT_EV_PLANNING_ENABLED,
    OPT_PLANNING_MODE,
    OPT_AUTOMATIC_SETUP,
    PLANNING_MODE_DISABLED,
    PLANNING_MODE_LIVE,
    PLANNING_MODE_DEMO,
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
    """Configure common homes in one screen and advanced homes in small steps."""

    _pending: dict[str, Any]

    def _current(self) -> dict[str, Any]:
        return resolved_options(
            self.hass,
            {**dict(self.config_entry.options), **getattr(self, "_pending", {})},
        )

    @staticmethod
    def _optional(key: str, current: dict[str, Any]) -> vol.Optional:
        value = current.get(key)
        return vol.Optional(key, default=value) if value not in (None, "", []) else vol.Optional(key)

    def _entity_schema(
        self,
        keys: list[tuple[str, str | None, bool]],
    ) -> vol.Schema:
        current = self._current()
        schema: dict[Any, Any] = {}
        for key, domain, multiple in keys:
            config = selector.EntitySelectorConfig(multiple=multiple)
            if domain is not None:
                config = selector.EntitySelectorConfig(domain=domain, multiple=multiple)
            schema[self._optional(key, current)] = selector.EntitySelector(config)
        return vol.Schema(schema)

    def _number_schema(self, keys: list[str]) -> vol.Schema:
        current = self._current()
        return vol.Schema({
            self._optional(key, current): vol.Coerce(float)
            for key in keys
        })

    def _save(self) -> ConfigFlowResult:
        options = resolved_options(self.hass, self._pending)
        options.pop("setup_method", None)
        return self.async_create_entry(title="", data=options)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._pending = {
                **optimisation_defaults(self.hass),
                **dict(self.config_entry.options),
                **user_input,
            }
            method = self._pending.get("setup_method", "automatic")
            self._pending[OPT_AUTOMATIC_SETUP] = method == "automatic"
            if self._pending[OPT_PLANNING_MODE] == PLANNING_MODE_LIVE and method == "automatic":
                self._pending = await async_discover_options(self.hass, self._pending)
                self._pending[OPT_PLANNING_MODE] = PLANNING_MODE_LIVE
                return self._save()
            if self._pending[OPT_PLANNING_MODE] != PLANNING_MODE_LIVE:
                return self._save()
            if method == "manual":
                return await self.async_step_metering()
            return self._save()

        current = self._current()
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    OPT_PLANNING_MODE,
                    default=current[OPT_PLANNING_MODE],
                ): selector.SelectSelector(selector.SelectSelectorConfig(
                    options=[
                        {"value": PLANNING_MODE_DISABLED, "label": "Off — monitoring only"},
                        {"value": PLANNING_MODE_LIVE, "label": "Live planning"},
                        {"value": PLANNING_MODE_DEMO, "label": "Demo — synthetic, never controls devices"},
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )),
                vol.Required(
                    "setup_method",
                    default="automatic" if current.get(OPT_AUTOMATIC_SETUP, True) else "manual",
                ): selector.SelectSelector(selector.SelectSelectorConfig(
                    options=[
                        {"value": "automatic", "label": "Automatic — use Energy Dashboard"},
                        {"value": "manual", "label": "Manual — review advanced mappings"},
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )),
            }),
        )

    async def async_step_metering(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._pending.update(user_input)
            return await self.async_step_categories()
        keys = [
            (f"{OPT_PREFIX_ENTITIES}{category}", "sensor", True)
            for category in (
                "grid_import", "grid_export", "solar_production",
                "total_consumption", "battery_charge", "battery_discharge",
            )
        ]
        return self.async_show_form(step_id="metering", data_schema=self._entity_schema(keys))

    async def async_step_categories(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._pending.update(user_input)
            return await self.async_step_forecasts()
        keys = [
            (f"{OPT_PREFIX_ENTITIES}{category}", "sensor", True)
            for category in (
                "heating", "hot_water", "cooling", "property_energy",
                "pool_heating", "ev_charging", "household",
            )
        ]
        return self.async_show_form(step_id="categories", data_schema=self._entity_schema(keys))

    async def async_step_forecasts(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._pending.update(user_input)
            return await self.async_step_battery()
        current = self._current()
        schema = dict(self._entity_schema([
            (OPT_SUPPLIER_IMPORT_PRICE, "sensor", False),
            (OPT_SUPPLIER_EXPORT_PRICE, "sensor", False),
            (OPT_PV_FORECAST_ENTITIES, "sensor", True),
            (OPT_SUPPLIER_IMPORT_FORECAST_ENTITY, "sensor", False),
            (OPT_SUPPLIER_EXPORT_FORECAST_ENTITY, "sensor", False),
            (OPT_GRID_EXPORT_POWER_ENTITY, "sensor", False),
        ]).schema)
        schema[self._optional(OPT_ELECTRICITY_PRICE_AREA, current)] = str
        return self.async_show_form(step_id="forecasts", data_schema=vol.Schema(schema))

    async def async_step_battery(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._pending.update(user_input)
            return await self.async_step_flexible_loads()
        current = self._current()
        schema = dict(self._entity_schema([(OPT_BATTERY_SOC_ENTITY, "sensor", False)]).schema)
        schema.update(self._number_schema([
            OPT_BATTERY_CAPACITY_KWH, OPT_BATTERY_CHARGE_MAX_W,
            OPT_BATTERY_DISCHARGE_MAX_W, OPT_BATTERY_MIN_SOC,
            OPT_BATTERY_MAX_SOC, OPT_BATTERY_TARGET_SOC,
            OPT_BATTERY_CHARGE_EFFICIENCY, OPT_BATTERY_DISCHARGE_EFFICIENCY,
            OPT_GRID_IMPORT_LIMIT_W, OPT_GRID_EXPORT_LIMIT_W,
            OPT_TERMINAL_SOC_MIN, OPT_TERMINAL_ENERGY_VALUE,
        ]).schema)
        schema[vol.Optional(
            OPT_BATTERY_TARGET_IS_HARD,
            default=bool(current[OPT_BATTERY_TARGET_IS_HARD]),
        )] = bool
        return self.async_show_form(step_id="battery", data_schema=vol.Schema(schema))

    async def async_step_flexible_loads(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._pending.update(user_input)
            return await self.async_step_pool()
        current = self._current()
        return self.async_show_form(step_id="flexible_loads", data_schema=vol.Schema({
            vol.Optional(OPT_POOL_PLANNING_ENABLED, default=bool(current[OPT_POOL_PLANNING_ENABLED])): bool,
            vol.Optional(OPT_BOILER_PLANNING_ENABLED, default=bool(current[OPT_BOILER_PLANNING_ENABLED])): bool,
            vol.Optional(OPT_EV_PLANNING_ENABLED, default=bool(current[OPT_EV_PLANNING_ENABLED])): bool,
        }))

    async def async_step_pool(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not self._pending.get(OPT_POOL_PLANNING_ENABLED):
            return await self.async_step_boiler()
        if user_input is not None:
            self._pending.update(user_input)
            return await self.async_step_boiler()
        current = self._current()
        schema = dict(self._entity_schema([(OPT_POOL_ENABLED_ENTITY, None, False)]).schema)
        schema.update(self._number_schema([OPT_POOL_POWER_W, OPT_POOL_MIN_RUN_SLOTS]).schema)
        schema[vol.Optional(OPT_POOL_DEADLINE, default=current[OPT_POOL_DEADLINE])] = str
        schema[vol.Optional(OPT_POOL_BASELINE_START, default=current[OPT_POOL_BASELINE_START])] = str
        return self.async_show_form(step_id="pool", data_schema=vol.Schema(schema))

    async def async_step_boiler(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not self._pending.get(OPT_BOILER_PLANNING_ENABLED):
            return await self.async_step_ev()
        if user_input is not None:
            self._pending.update(user_input)
            return await self.async_step_ev()
        current = self._current()
        schema = dict(self._number_schema([OPT_BOILER_POWER_W, OPT_BOILER_MIN_RUN_SLOTS]).schema)
        schema[vol.Optional(OPT_BOILER_DEADLINE, default=current[OPT_BOILER_DEADLINE])] = str
        schema[vol.Optional(OPT_BOILER_BASELINE_START, default=current[OPT_BOILER_BASELINE_START])] = str
        return self.async_show_form(step_id="boiler", data_schema=vol.Schema(schema))

    async def async_step_ev(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not self._pending.get(OPT_EV_PLANNING_ENABLED):
            return self._save()
        if user_input is not None:
            self._pending.update(user_input)
            return self._save()
        current = self._current()
        schema = dict(self._entity_schema([
            (OPT_EV_CONNECTED_ENTITY, None, False),
            (OPT_EV_SOC_ENTITY, "sensor", False),
            (OPT_EV_TARGET_SOC_ENTITY, None, False),
            (OPT_EV_DEPARTURE_ENTITY, None, False),
            (OPT_EV_CHARGE_CURRENT_ENTITY, None, False),
            (OPT_EV_ENERGY_REMAINING_ENTITY, "sensor", False),
        ]).schema)
        schema.update(self._number_schema([
            OPT_EV_POWER_W, OPT_EV_BATTERY_KWH, OPT_EV_CHARGE_EFFICIENCY,
            OPT_EV_MIN_RUN_SLOTS, OPT_EV_PHASE_COUNT, OPT_EV_VOLTAGE,
        ]).schema)
        schema[vol.Optional(OPT_EV_DEFAULT_DEPARTURE, default=current[OPT_EV_DEFAULT_DEPARTURE])] = str
        return self.async_show_form(step_id="ev", data_schema=vol.Schema(schema))
