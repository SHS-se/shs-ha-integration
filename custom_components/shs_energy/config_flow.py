"""Config and options flow for Smart Home Solutions Energy."""

from __future__ import annotations

from datetime import datetime, timezone
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
    async_discover_configuration,
    optimisation_defaults,
    resolved_options,
    suggest_device_control_mapping,
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
    OPT_BATTERY_EXPORT_ENABLED,
    OPT_BATTERY_EXPORT_MIN_PRICE,
    OPT_BATTERY_EXPORT_RESERVE_SOC,
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
    OPT_BOILER_DEFERRABLE_CONFIRMED,
    OPT_BOILER_MAX_INHIBIT_SLOTS,
    OPT_EV_CONNECTED_ENTITY,
    OPT_EV_SOC_ENTITY,
    OPT_EV_TARGET_SOC_ENTITY,
    OPT_EV_DEPARTURE_ENTITY,
    OPT_EV_POWER_W,
    OPT_EV_BATTERY_KWH,
    OPT_EV_CHARGE_EFFICIENCY,
    OPT_EV_MIN_RUN_SLOTS,
    OPT_EV_CHARGE_CURRENT_ENTITY,
    OPT_EV_MIN_CURRENT_A,
    OPT_EV_MAX_CURRENT_A,
    OPT_EV_CURRENT_STEP_A,
    OPT_EV_ENERGY_REMAINING_ENTITY,
    OPT_EV_PHASE_COUNT,
    OPT_EV_VOLTAGE,
    OPT_EV_DEFAULT_DEPARTURE,
    OPT_EV_DEFERRABLE_CONFIRMED,
    OPT_EV_ELECTRICAL_CONFIRMED,
    OPT_POOL_PLANNING_ENABLED,
    OPT_POOL_DEFERRABLE_CONFIRMED,
    OPT_BOILER_PLANNING_ENABLED,
    OPT_EV_PLANNING_ENABLED,
    OPT_PLANNING_MODE,
    OPT_AUTOMATIC_SETUP,
    OPT_CONFIGURATION_REVIEWED_AT,
    OPT_DEVICE_CONTROL_MAPPINGS,
    PLANNING_MODE_DISABLED,
    PLANNING_MODE_LIVE,
    OPT_SUPPLIER_EXPORT_PRICE,
    OPT_SUPPLIER_IMPORT_PRICE,
)
from .device_controls import mapping_errors, mapping_report


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
    _discovery: dict[str, Any]
    _requested_devices: list[dict[str, Any]]
    _device_queue: list[dict[str, Any]]
    _device_index: int
    _portal_refreshed: bool

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

    def _device_mappings(self) -> dict[str, dict[str, Any]]:
        value = self._current().get(OPT_DEVICE_CONTROL_MAPPINGS, {})
        return {
            str(key): dict(mapping)
            for key, mapping in value.items()
            if isinstance(mapping, dict)
        } if isinstance(value, dict) else {}

    def _mapping_defaults(self, device: dict[str, Any]) -> dict[str, Any]:
        """Reuse saved values and relevant legacy values during migration."""
        control_type = str(device["control_type"])
        saved = self._device_mappings().get(device["key"])
        if saved and saved.get("control_type") == control_type:
            return saved
        current = self._current()
        defaults = suggest_device_control_mapping(
            self.hass, device, control_type
        )
        if control_type == "current_limit":
            for target, source in (
                ("current_control_entity_id", OPT_EV_CHARGE_CURRENT_ENTITY),
                ("connected_entity_id", OPT_EV_CONNECTED_ENTITY),
                ("soc_entity_id", OPT_EV_SOC_ENTITY),
                ("target_soc_entity_id", OPT_EV_TARGET_SOC_ENTITY),
                ("departure_entity_id", OPT_EV_DEPARTURE_ENTITY),
                ("energy_remaining_entity_id", OPT_EV_ENERGY_REMAINING_ENTITY),
                ("battery_capacity_kwh", OPT_EV_BATTERY_KWH),
                ("min_current_a", OPT_EV_MIN_CURRENT_A),
                ("max_current_a", OPT_EV_MAX_CURRENT_A),
                ("current_step_a", OPT_EV_CURRENT_STEP_A),
                ("phase_count", OPT_EV_PHASE_COUNT),
                ("voltage", OPT_EV_VOLTAGE),
                ("min_run_slots", OPT_EV_MIN_RUN_SLOTS),
            ):
                if current.get(source) not in (None, "", []):
                    defaults[target] = current[source]
        elif control_type == "permit_inhibit":
            defaults["max_inhibit_slots"] = current[OPT_BOILER_MAX_INHIBIT_SLOTS]
            if current.get(OPT_BOILER_POWER_W) not in (None, ""):
                defaults["power_w"] = current[OPT_BOILER_POWER_W]
        elif control_type == "switch_schedule":
            defaults["min_run_slots"] = current[OPT_POOL_MIN_RUN_SLOTS]
            if current.get(OPT_POOL_ENABLED_ENTITY):
                defaults["availability_entity_id"] = current[OPT_POOL_ENABLED_ENTITY]
            if current.get(OPT_POOL_POWER_W) not in (None, ""):
                defaults["power_w"] = current[OPT_POOL_POWER_W]
        return defaults

    @staticmethod
    def _field(
        schema: dict[Any, Any],
        defaults: dict[str, Any],
        key: str,
        value_selector: Any,
        *,
        required: bool,
        default: Any = None,
    ) -> None:
        value = defaults.get(key, default)
        marker: Any
        if required:
            marker = (
                vol.Required(key, default=value)
                if value not in (None, "", [])
                else vol.Required(key)
            )
        else:
            marker = (
                vol.Optional(key, default=value)
                if value not in (None, "", [])
                else vol.Optional(key)
            )
        schema[marker] = value_selector

    def _device_mapping_schema(self, device: dict[str, Any]) -> vol.Schema:
        control_type = str(device["control_type"])
        defaults = self._mapping_defaults(device)
        schema: dict[Any, Any] = {}
        entity = lambda multiple=False: selector.EntitySelector(
            selector.EntitySelectorConfig(multiple=multiple)
        )
        positive = selector.NumberSelector(selector.NumberSelectorConfig(
            min=0.01, step=0.01, mode=selector.NumberSelectorMode.BOX
        ))
        whole = selector.NumberSelector(selector.NumberSelectorConfig(
            min=1, step=1, mode=selector.NumberSelectorMode.BOX
        ))

        if control_type == "setpoint":
            self._field(schema, defaults, "temperature_entity_id", entity(), required=True)
            self._field(schema, defaults, "setpoint_entity_id", entity(), required=False)
            self._field(schema, defaults, "comfort_high_entity_id", entity(), required=False)
            self._field(schema, defaults, "comfort_low_entity_id", entity(), required=False)
            self._field(schema, defaults, "actuator_entity_ids", entity(True), required=True)
            self._field(
                schema, defaults, "companion_actuator_entity_ids",
                entity(True), required=False,
            )
            self._field(schema, defaults, "power_entity_id", entity(), required=False)
            self._field(schema, defaults, "override_entity_id", entity(), required=False)
            self._field(schema, defaults, "override_timer_entity_id", entity(), required=False)
        elif control_type == "permit_inhibit":
            self._field(schema, defaults, "actuator_entity_ids", entity(True), required=True)
            self._field(schema, defaults, "availability_entity_id", entity(), required=False)
            self._field(schema, defaults, "power_entity_id", entity(), required=False)
            self._field(schema, defaults, "power_w", positive, required=False)
            self._field(schema, defaults, "max_inhibit_slots", whole, required=True, default=4)
        elif control_type == "switch_schedule":
            self._field(schema, defaults, "actuator_entity_ids", entity(True), required=True)
            self._field(
                schema, defaults, "companion_actuator_entity_ids",
                entity(True), required=False,
            )
            self._field(schema, defaults, "availability_entity_id", entity(), required=False)
            self._field(schema, defaults, "power_entity_id", entity(), required=False)
            self._field(schema, defaults, "power_w", positive, required=False)
            self._field(schema, defaults, "min_run_slots", whole, required=True, default=4)
        elif control_type == "variable_power":
            self._field(schema, defaults, "power_control_entity_id", entity(), required=True)
            self._field(schema, defaults, "availability_entity_id", entity(), required=False)
            self._field(schema, defaults, "power_entity_id", entity(), required=False)
        elif control_type == "current_limit":
            for key in (
                "current_control_entity_id", "connected_entity_id", "soc_entity_id",
                "target_soc_entity_id",
            ):
                self._field(schema, defaults, key, entity(), required=True)
            for key in (
                "departure_entity_id", "energy_remaining_entity_id", "power_entity_id",
            ):
                self._field(schema, defaults, key, entity(), required=False)
            for key, default in (
                ("battery_capacity_kwh", None), ("min_current_a", 6),
                ("max_current_a", 16), ("current_step_a", 1),
                ("phase_count", 3), ("voltage", 230), ("min_run_slots", 2),
            ):
                self._field(
                    schema, defaults, key,
                    whole if key in ("phase_count", "min_run_slots") else positive,
                    required=True, default=default,
                )
        return vol.Schema(schema)

    def _queue_device_controls(self, devices: list[dict[str, Any]]) -> None:
        self._device_queue = devices
        self._device_index = 0

    async def async_step_device_control(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure one website-requested device using its control contract."""
        device = self._device_queue[self._device_index]
        errors: dict[str, str] = {}
        if user_input is not None:
            mapping = {"control_type": device["control_type"], **user_input}
            if mapping_errors(mapping, str(device["control_type"])):
                errors["base"] = "invalid_device_mapping"
            else:
                mappings = self._device_mappings()
                mappings[device["key"]] = mapping
                self._pending[OPT_DEVICE_CONTROL_MAPPINGS] = mappings
                self._device_index += 1
                if self._device_index >= len(self._device_queue):
                    return self._save()
                return await self.async_step_device_control()
        return self.async_show_form(
            step_id="device_control",
            data_schema=self._device_mapping_schema(device),
            errors=errors,
            description_placeholders={
                "device_name": str(device.get("name") or device["key"]),
                "control_type": str(device["control_type"]).replace("_", " "),
                "position": str(self._device_index + 1),
                "total": str(len(self._device_queue)),
            },
        )

    def _save(self) -> ConfigFlowResult:
        options = resolved_options(self.hass, self._pending)
        options.pop("setup_method", None)
        if options.get(OPT_PLANNING_MODE) == PLANNING_MODE_LIVE:
            options[OPT_CONFIGURATION_REVIEWED_AT] = datetime.now(
                timezone.utc
            ).isoformat()
        return self.async_create_entry(title="", data=options)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if not hasattr(self, "_pending"):
            self._pending = {
                **optimisation_defaults(self.hass),
                **dict(self.config_entry.options),
            }
        if not hasattr(self, "_requested_devices"):
            self._requested_devices = []
        if not getattr(self, "_portal_refreshed", False):
            try:
                self._requested_devices = (
                    await self.config_entry.runtime_data.async_refresh_device_configuration()
                )
            except (ShsApiError, KeyError, TypeError, ValueError):
                errors["base"] = "website_configuration_unavailable"
                user_input = None
            else:
                self._portal_refreshed = True
                mappings = self._device_mappings()
                pending = [
                    device for device in self._requested_devices
                    if mapping_report(
                        device.get("control_type"), mappings.get(device["key"])
                    )["mapping_status"] != "ready"
                ]
                if pending:
                    self._queue_device_controls(pending)
                    return await self.async_step_device_control()
        if user_input is not None:
            self._pending = {
                **optimisation_defaults(self.hass),
                **dict(self.config_entry.options),
                **user_input,
            }
            method = self._pending.get("setup_method", "automatic")
            self._pending[OPT_AUTOMATIC_SETUP] = method == "automatic"
            if method == "device_controls":
                if self._requested_devices:
                    self._queue_device_controls(self._requested_devices)
                    return await self.async_step_device_control()
                return self._save()
            if self._pending[OPT_PLANNING_MODE] == PLANNING_MODE_LIVE and method == "automatic":
                mappings = self._pending[OPT_DEVICE_CONTROL_MAPPINGS]
                self._discovery = await async_discover_configuration(
                    self.hass, self._pending
                )
                self._pending = self._discovery["configuration"]
                self._pending[OPT_DEVICE_CONTROL_MAPPINGS] = mappings
                self._pending[OPT_PLANNING_MODE] = PLANNING_MODE_LIVE
                return await self.async_step_discovery_review()
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
                        {"value": "device_controls", "label": "Website-requested device controls"},
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )),
            }),
            errors=errors,
        )

    def _discovery_summary(self) -> str:
        capabilities = self._discovery["capabilities"]
        options = self._discovery["configuration"]
        categories = [
            f"{category.replace('_', ' ')}: "
            + ", ".join(options.get(f"{OPT_PREFIX_ENTITIES}{category}", []))
            for category in (
                "grid_import",
                "grid_export",
                "solar_production",
                "total_consumption",
                "pool_heating",
                "hot_water",
                "ev_charging",
            )
            if options.get(f"{OPT_PREFIX_ENTITIES}{category}")
        ]
        lines = [
            f"Energy Dashboard: {capabilities['metering']['detected']} aggregate meters",
            *(categories or ["No aggregate Energy Dashboard meters were found"]),
            f"PV forecast: {'found' if capabilities['pv']['candidate'] else 'not found (optional)'}",
            f"Battery: {'ready' if capabilities['battery']['ready'] else 'not ready or not installed (optional)'}",
            f"Prices: {capabilities['prices']['area'] or 'price area needs review'}",
        ]
        evidence = self._discovery["evidence"]
        for key, label in (
            (OPT_PV_FORECAST_ENTITIES, "PV source"),
            (OPT_BATTERY_SOC_ENTITY, "Battery SOC"),
            (OPT_BATTERY_CAPACITY_KWH, "Battery capacity"),
            (OPT_GRID_IMPORT_LIMIT_W, "Grid import limit"),
            (OPT_GRID_EXPORT_LIMIT_W, "Grid export limit"),
            (OPT_GRID_EXPORT_POWER_ENTITY, "Reactive export meter"),
            (OPT_POOL_ENABLED_ENTITY, "Pool enable gate"),
            (OPT_POOL_POWER_W, "Pool observed power"),
            (OPT_EV_CONNECTED_ENTITY, "EV connection"),
            (OPT_EV_SOC_ENTITY, "EV SOC"),
            (OPT_EV_TARGET_SOC_ENTITY, "EV target"),
            (OPT_EV_CHARGE_CURRENT_ENTITY, "EV current control"),
            (OPT_EV_MIN_CURRENT_A, "EV usable minimum current"),
            (OPT_EV_MAX_CURRENT_A, "EV maximum current"),
            (OPT_EV_CURRENT_STEP_A, "EV current increment"),
            (OPT_EV_BATTERY_KWH, "EV capacity"),
        ):
            if key not in evidence:
                continue
            value = options.get(key)
            if isinstance(value, list):
                value = ", ".join(value)
            lines.append(
                f"{label}: {value} "
                f"[{evidence[key]['source']}, {evidence[key]['confidence']} confidence]"
            )
        for name, label in (
            ("pool", "Pool heating"),
            ("boiler", "Water heating"),
            ("ev", "EV charging"),
        ):
            capability = capabilities[name]
            if not capability["candidate"]:
                continue
            state = (
                "all inputs found"
                if capability["ready_after_review"]
                else "missing: " + ", ".join(capability["missing"])
            )
            lines.append(f"{label}: candidate only — {state}")
        ev_current = capabilities["ev"].get("current_control")
        if ev_current:
            lines.append(
                "EV current control: "
                f"{ev_current['entity_id']} "
                f"(entity reports {ev_current['raw_min_a']}–"
                f"{ev_current['raw_max_a']} A; proposed usable range "
                f"{ev_current['proposed_min_a']}–"
                f"{ev_current['proposed_max_a']} A, step "
                f"{ev_current['proposed_step_a']} A). Confirm this range, "
                "phases and voltage."
            )
        return "\n".join(lines)

    async def async_step_discovery_review(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Require a human decision before any discovered load is deferrable."""
        pairs = (
            ("pool", OPT_POOL_PLANNING_ENABLED, OPT_POOL_DEFERRABLE_CONFIRMED),
            (
                "boiler",
                OPT_BOILER_PLANNING_ENABLED,
                OPT_BOILER_DEFERRABLE_CONFIRMED,
            ),
            ("ev", OPT_EV_PLANNING_ENABLED, OPT_EV_DEFERRABLE_CONFIRMED),
        )
        if user_input is not None:
            for _name, enabled_key, confirmation_key in pairs:
                self._pending[enabled_key] = bool(
                    user_input.get(enabled_key, False)
                )
                self._pending[confirmation_key] = False
            self._pending[OPT_EV_ELECTRICAL_CONFIRMED] = False
            return await self.async_step_pool()

        current = self._current()
        schema: dict[Any, Any] = {}
        for name, enabled_key, confirmation_key in pairs:
            if not self._discovery["capabilities"][name]["candidate"]:
                continue
            schema[vol.Optional(
                enabled_key,
                default=bool(
                    current.get(enabled_key) and current.get(confirmation_key)
                ),
            )] = bool
        return self.async_show_form(
            step_id="discovery_review",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "discovery_summary": self._discovery_summary()
            },
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
            OPT_BATTERY_EXPORT_RESERVE_SOC, OPT_BATTERY_EXPORT_MIN_PRICE,
            OPT_GRID_IMPORT_LIMIT_W, OPT_GRID_EXPORT_LIMIT_W,
            OPT_TERMINAL_SOC_MIN, OPT_TERMINAL_ENERGY_VALUE,
        ]).schema)
        schema[vol.Optional(
            OPT_BATTERY_TARGET_IS_HARD,
            default=bool(current[OPT_BATTERY_TARGET_IS_HARD]),
        )] = bool
        schema[vol.Optional(
            OPT_BATTERY_EXPORT_ENABLED,
            default=bool(current[OPT_BATTERY_EXPORT_ENABLED]),
        )] = bool
        return self.async_show_form(step_id="battery", data_schema=vol.Schema(schema))

    async def async_step_flexible_loads(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._pending.update(user_input)
            self._pending[OPT_POOL_DEFERRABLE_CONFIRMED] = False
            self._pending[OPT_BOILER_DEFERRABLE_CONFIRMED] = False
            self._pending[OPT_EV_DEFERRABLE_CONFIRMED] = False
            self._pending[OPT_EV_ELECTRICAL_CONFIRMED] = False
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
            self._pending[OPT_POOL_DEFERRABLE_CONFIRMED] = True
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
            self._pending[OPT_BOILER_DEFERRABLE_CONFIRMED] = True
            return await self.async_step_ev()
        current = self._current()
        schema = dict(self._number_schema([
            OPT_BOILER_POWER_W, OPT_BOILER_MAX_INHIBIT_SLOTS
        ]).schema)
        return self.async_show_form(step_id="boiler", data_schema=vol.Schema(schema))

    async def async_step_ev(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not self._pending.get(OPT_EV_PLANNING_ENABLED):
            return self._save()
        if user_input is not None:
            self._pending.update(user_input)
            self._pending[OPT_EV_DEFERRABLE_CONFIRMED] = True
            self._pending[OPT_EV_ELECTRICAL_CONFIRMED] = True
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
            OPT_EV_MIN_RUN_SLOTS, OPT_EV_MIN_CURRENT_A,
            OPT_EV_MAX_CURRENT_A, OPT_EV_CURRENT_STEP_A,
            OPT_EV_PHASE_COUNT, OPT_EV_VOLTAGE,
        ]).schema)
        schema[vol.Optional(OPT_EV_DEFAULT_DEPARTURE, default=current[OPT_EV_DEFAULT_DEPARTURE])] = str
        return self.async_show_form(step_id="ev", data_schema=vol.Schema(schema))
