"""Full-page Home Assistant configuration surface for SHS Energy."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from math import isfinite
from pathlib import Path
import re
from typing import Any

import voluptuous as vol

from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import const as shs_const
from .api import ShsApiError
from .configuration import (
    async_discover_configuration,
    optimisation_defaults,
    resolved_options,
    suggest_device_control_mapping,
)
from .device_controls import mapping_report

_LOGGER = logging.getLogger(__name__)

PANEL_URL = "shs-energy"
PANEL_ELEMENT = "shs-energy-config-panel"
STATIC_URL = "/shs_energy_frontend"
FRONTEND_DIR = Path(__file__).parent / "frontend"


def _field(
    key: str,
    label: str,
    kind: str,
    *,
    help_text: str = "",
    domains: tuple[str, ...] = (),
    required: bool = False,
    choices: tuple[tuple[str, str], ...] = (),
    unit: str | None = None,
    step: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    scale: float | None = None,
) -> dict[str, Any]:
    """Describe one field for the deliberately generic panel renderer."""
    result: dict[str, Any] = {
        "key": key,
        "label": label,
        "kind": kind,
        "help": help_text,
        "required": required,
    }
    if domains:
        result["domains"] = list(domains)
    if choices:
        result["choices"] = [
            {"value": value, "label": choice_label}
            for value, choice_label in choices
        ]
    if unit is not None:
        result["unit"] = unit
    if step is not None:
        result["step"] = step
    if minimum is not None:
        result["minimum"] = minimum
    if maximum is not None:
        result["maximum"] = maximum
    if scale is not None:
        result["scale"] = scale
    return result


CONTROL_FIELDS: dict[str, tuple[dict[str, Any], ...]] = {
    "setpoint": (
        _field(
            "temperature_entity_id",
            "Room or process temperature",
            "entity",
            domains=("sensor", "climate"),
            required=True,
            help_text="The measured temperature used to learn how this zone responds.",
        ),
        _field(
            "setpoint_entity_id",
            "Direct setpoint",
            "entity",
            domains=("number", "input_number", "climate"),
            help_text="Use this when one entity contains the current target temperature.",
        ),
        _field(
            "comfort_high_entity_id",
            "Scheduled comfort temperature",
            "entity",
            domains=("number", "input_number"),
            help_text="Use both comfort and setback when there is no direct setpoint entity.",
        ),
        _field(
            "comfort_low_entity_id",
            "Scheduled setback temperature",
            "entity",
            domains=("number", "input_number"),
        ),
        _field(
            "actuator_entity_ids",
            "Controlled heater or climate actuator(s)",
            "entities",
            domains=("switch", "climate", "input_boolean"),
            required=True,
            help_text="These entities reveal actual heating duty. This page does not operate them.",
        ),
        _field(
            "companion_actuator_entity_ids",
            "Required companion actuator(s)",
            "entities",
            domains=("switch", "climate", "input_boolean"),
            help_text="For coupled equipment such as a circulation pump.",
        ),
        _field(
            "power_entity_id",
            "Measured power",
            "entity",
            domains=("sensor",),
            help_text="Optional when the Energy Dashboard meter already supplies sufficient history.",
        ),
        _field(
            "override_entity_id",
            "Manual override state",
            "entity",
            domains=("input_boolean", "input_text", "select", "sensor"),
        ),
        _field(
            "override_timer_entity_id",
            "Manual override timer",
            "entity",
            domains=("input_number", "timer", "sensor"),
        ),
    ),
    "permit_inhibit": (
        _field(
            "actuator_entity_ids",
            "Permit/inhibit actuator(s)",
            "entities",
            domains=("switch", "input_boolean", "climate"),
            required=True,
            help_text="The local thermostat keeps ownership of the duty cycle.",
        ),
        _field(
            "availability_entity_id",
            "Availability or season state",
            "entity",
            domains=("binary_sensor", "input_boolean", "switch"),
        ),
        _field(
            "power_entity_id",
            "Measured power",
            "entity",
            domains=("sensor",),
        ),
        _field("power_w", "Reviewed rated power", "number", unit="W", minimum=1, step=1),
        _field(
            "max_inhibit_slots",
            "Maximum continuous inhibit",
            "number",
            unit="15-minute slots",
            minimum=1,
            step=1,
            required=True,
        ),
    ),
    "switch_schedule": (
        _field(
            "actuator_entity_ids",
            "Scheduled switch actuator(s)",
            "entities",
            domains=("switch", "input_boolean", "climate"),
            required=True,
        ),
        _field(
            "companion_actuator_entity_ids",
            "Required companion actuator(s)",
            "entities",
            domains=("switch", "input_boolean", "climate"),
        ),
        _field(
            "availability_entity_id",
            "Availability or season state",
            "entity",
            domains=("binary_sensor", "input_boolean", "switch"),
        ),
        _field("power_entity_id", "Measured power", "entity", domains=("sensor",)),
        _field("power_w", "Reviewed rated power", "number", unit="W", minimum=1, step=1),
        _field(
            "min_run_slots",
            "Minimum run",
            "number",
            unit="15-minute slots",
            minimum=1,
            step=1,
            required=True,
        ),
    ),
    "variable_power": (
        _field(
            "power_control_entity_id",
            "Power control",
            "entity",
            domains=("number", "input_number"),
            required=True,
        ),
        _field(
            "availability_entity_id",
            "Availability state",
            "entity",
            domains=("binary_sensor", "input_boolean", "switch"),
        ),
        _field("power_entity_id", "Measured power", "entity", domains=("sensor",)),
    ),
    "current_limit": (
        _field(
            "current_control_entity_id",
            "Charging-current control",
            "entity",
            domains=("number", "input_number"),
            required=True,
        ),
        _field(
            "connected_entity_id",
            "Vehicle connected state",
            "entity",
            domains=("binary_sensor", "input_boolean", "sensor"),
            required=True,
        ),
        _field(
            "soc_entity_id",
            "Vehicle battery state of charge",
            "entity",
            domains=("sensor",),
            required=True,
        ),
        _field(
            "target_soc_entity_id",
            "Vehicle target state of charge",
            "entity",
            domains=("number", "input_number", "sensor"),
            required=True,
        ),
        _field(
            "departure_entity_id",
            "Departure time",
            "entity",
            domains=("sensor", "input_datetime"),
        ),
        _field(
            "energy_remaining_entity_id",
            "Energy remaining",
            "entity",
            domains=("sensor",),
        ),
        _field("power_entity_id", "Measured charging power", "entity", domains=("sensor",)),
        _field(
            "battery_capacity_kwh",
            "Usable vehicle battery capacity",
            "number",
            unit="kWh",
            minimum=0.1,
            step=0.1,
            required=True,
        ),
        _field("min_current_a", "Minimum charging current", "number", unit="A", minimum=0.1, step=0.1, required=True),
        _field("max_current_a", "Maximum charging current", "number", unit="A", minimum=0.1, step=0.1, required=True),
        _field("current_step_a", "Charging-current step", "number", unit="A", minimum=0.1, step=0.1, required=True),
        _field("phase_count", "Charging phases", "number", minimum=1, maximum=3, step=1, required=True),
        _field("voltage", "Charging voltage", "number", unit="V", minimum=1, step=1, required=True),
        _field("min_run_slots", "Minimum run", "number", unit="15-minute slots", minimum=1, step=1, required=True),
    ),
}


def _configuration_sections() -> list[dict[str, Any]]:
    """Return the editable non-device configuration grouped for the panel."""
    c = shs_const
    category_labels = (
        ("grid_import", "Grid import"),
        ("grid_export", "Grid export"),
        ("solar_production", "Solar production"),
        ("total_consumption", "Whole-home consumption"),
        ("battery_charge", "Battery charge energy"),
        ("battery_discharge", "Battery discharge energy"),
        ("heating", "Heating energy"),
        ("hot_water", "Hot-water energy"),
        ("cooling", "Cooling energy"),
        ("property_energy", "Property energy"),
        ("pool_heating", "Pool-heating energy"),
        ("ev_charging", "EV-charging energy"),
        ("household", "Household energy"),
    )
    return [
        {
            "id": "planning",
            "tab": "overview",
            "title": "Planning",
            "description": "Monitoring remains active when planning is off. Saving configuration never operates a device.",
            "fields": [
                _field(
                    c.OPT_PLANNING_MODE,
                    "Planning mode",
                    "select",
                    choices=((c.PLANNING_MODE_DISABLED, "Off — monitoring only"), (c.PLANNING_MODE_LIVE, "Live planning")),
                    required=True,
                )
            ],
        },
        {
            "id": "metering",
            "tab": "inputs",
            "title": "Energy Dashboard meters",
            "description": "Every listed sensor is summed into its category. Device meters remain classified separately by the website.",
            "fields": [
                _field(
                    f"{c.OPT_PREFIX_ENTITIES}{category}",
                    label,
                    "entities",
                    domains=("sensor",),
                )
                for category, label in category_labels
            ],
        },
        {
            "id": "prices_forecasts",
            "tab": "inputs",
            "title": "Prices, solar and electrical measurements",
            "description": "Import and export remain separate because buying and selling have different values.",
            "fields": [
                _field(c.OPT_SUPPLIER_IMPORT_PRICE, "Current supplier import price", "entity", domains=("sensor",)),
                _field(c.OPT_SUPPLIER_EXPORT_PRICE, "Current supplier export price", "entity", domains=("sensor",)),
                _field(c.OPT_PV_FORECAST_ENTITIES, "Solar forecast", "entities", domains=("sensor",)),
                _field(c.OPT_SUPPLIER_IMPORT_FORECAST_ENTITY, "Import-price forecast", "entity", domains=("sensor",)),
                _field(c.OPT_SUPPLIER_EXPORT_FORECAST_ENTITY, "Export-price forecast", "entity", domains=("sensor",)),
                _field(c.OPT_ELECTRICITY_PRICE_AREA, "Swedish price area", "select", choices=(("SE1", "SE1"), ("SE2", "SE2"), ("SE3", "SE3"), ("SE4", "SE4"))),
                _field(c.OPT_GRID_EXPORT_POWER_ENTITY, "Instantaneous grid-export power", "entity", domains=("sensor",)),
                _field(c.OPT_PV_FORECAST_LATITUDE, "Solar forecast latitude", "number", step=0.00001),
                _field(c.OPT_PV_FORECAST_LONGITUDE, "Solar forecast longitude", "number", step=0.00001),
            ],
        },
        {
            "id": "thermal_sources",
            "tab": "thermal",
            "title": "Shared outdoor conditions",
            "description": "Room sensors and actuators are mapped on each setpoint-controlled device. These shared sources let the website learn weather response and project it forward.",
            "fields": [
                _field(c.OPT_OUTDOOR_TEMPERATURE_ENTITY, "Measured outdoor temperature", "entity", domains=("sensor",)),
                _field(c.OPT_WEATHER_FORECAST_ENTITY, "Outdoor weather forecast", "entity", domains=("weather",)),
            ],
        },
        {
            "id": "house_battery",
            "tab": "storage",
            "title": "House battery",
            "description": "Export is a customer preference and remains advisory until a reviewed local battery executor exists.",
            "fields": [
                _field(c.OPT_BATTERY_SOC_ENTITY, "Battery state of charge", "entity", domains=("sensor",)),
                _field(c.OPT_BATTERY_CAPACITY_KWH, "Usable capacity", "number", unit="kWh", minimum=0.1, step=0.1),
                _field(c.OPT_BATTERY_CHARGE_MAX_W, "Maximum charge power", "number", unit="W", minimum=1, step=1),
                _field(c.OPT_BATTERY_DISCHARGE_MAX_W, "Maximum discharge power", "number", unit="W", minimum=1, step=1),
                _field(c.OPT_BATTERY_MIN_SOC, "Minimum SOC", "number", unit="%", minimum=0, maximum=100, step=1, scale=100),
                _field(c.OPT_BATTERY_MAX_SOC, "Maximum SOC", "number", unit="%", minimum=0, maximum=100, step=1, scale=100),
                _field(c.OPT_BATTERY_TARGET_SOC, "Preferred terminal SOC", "number", unit="%", minimum=0, maximum=100, step=1, scale=100),
                _field(c.OPT_BATTERY_TARGET_IS_HARD, "Make terminal target mandatory", "toggle"),
                _field(c.OPT_BATTERY_CHARGE_EFFICIENCY, "Charge efficiency", "number", unit="%", minimum=1, maximum=100, step=1, scale=100),
                _field(c.OPT_BATTERY_DISCHARGE_EFFICIENCY, "Discharge efficiency", "number", unit="%", minimum=1, maximum=100, step=1, scale=100),
                _field(c.OPT_BATTERY_EXPORT_ENABLED, "Allow planned battery export", "toggle"),
                _field(c.OPT_BATTERY_EXPORT_RESERVE_SOC, "Export reserve SOC", "number", unit="%", minimum=0, maximum=100, step=1, scale=100),
                _field(c.OPT_BATTERY_EXPORT_MIN_PRICE, "Minimum export price", "number", unit="SEK/kWh", minimum=0, step=0.01),
            ],
        },
        {
            "id": "electrical_limits",
            "tab": "storage",
            "title": "Electrical and horizon limits",
            "fields": [
                _field(c.OPT_GRID_IMPORT_LIMIT_W, "Grid import limit", "number", unit="W", minimum=1, step=1),
                _field(c.OPT_GRID_EXPORT_LIMIT_W, "Grid export limit", "number", unit="W", minimum=1, step=1),
                _field(c.OPT_TERMINAL_SOC_MIN, "Hard minimum terminal SOC", "number", unit="%", minimum=0, maximum=100, step=1, scale=100),
                _field(c.OPT_TERMINAL_ENERGY_VALUE, "Remaining battery value", "number", unit="SEK/kWh", minimum=0, step=0.01),
            ],
        },
        {
            "id": "pool",
            "tab": "devices",
            "title": "Pool service window",
            "description": "These portfolio constraints supplement the pool device's local switch mapping.",
            "fields": [
                _field(c.OPT_POOL_PLANNING_ENABLED, "Include pool in planning", "toggle"),
                _field(c.OPT_POOL_DEFERRABLE_CONFIRMED, "I confirm the pool load is deferrable", "toggle"),
                _field(c.OPT_POOL_ENABLED_ENTITY, "Pool season or enabled state", "entity"),
                _field(c.OPT_POOL_POWER_W, "Heater plus required pump power", "number", unit="W", minimum=1, step=1),
                _field(c.OPT_POOL_MIN_RUN_SLOTS, "Minimum run", "number", unit="15-minute slots", minimum=1, step=1),
                _field(c.OPT_POOL_DEADLINE, "Daily deadline", "time"),
                _field(c.OPT_POOL_BASELINE_START, "Baseline preferred start", "time"),
            ],
        },
        {
            "id": "hot_water",
            "tab": "devices",
            "title": "Water-heater safety envelope",
            "description": "The thermostat owns the duty cycle. Planning may only permit or temporarily inhibit it.",
            "fields": [
                _field(c.OPT_BOILER_PLANNING_ENABLED, "Include water heating in planning", "toggle"),
                _field(c.OPT_BOILER_DEFERRABLE_CONFIRMED, "I confirm temporary inhibit is safe", "toggle"),
                _field(c.OPT_BOILER_POWER_W, "Reviewed element power", "number", unit="W", minimum=1, step=1),
                _field(c.OPT_BOILER_MAX_INHIBIT_SLOTS, "Maximum continuous inhibit", "number", unit="15-minute slots", minimum=1, step=1),
            ],
        },
        {
            "id": "ev",
            "tab": "storage",
            "title": "EV obligation",
            "description": "Vehicle SOC and target determine how much energy the car can accept and its deadline priority.",
            "fields": [
                _field(c.OPT_EV_PLANNING_ENABLED, "Include EV charging in planning", "toggle"),
                _field(c.OPT_EV_DEFERRABLE_CONFIRMED, "I confirm EV charging is deferrable", "toggle"),
                _field(c.OPT_EV_ELECTRICAL_CONFIRMED, "I confirm phases, voltage and current limits", "toggle"),
                _field(c.OPT_EV_CONNECTED_ENTITY, "Vehicle connected state", "entity"),
                _field(c.OPT_EV_SOC_ENTITY, "Vehicle battery SOC", "entity", domains=("sensor",)),
                _field(c.OPT_EV_TARGET_SOC_ENTITY, "Vehicle target SOC", "entity"),
                _field(c.OPT_EV_DEPARTURE_ENTITY, "Departure time", "entity"),
                _field(c.OPT_EV_CHARGE_CURRENT_ENTITY, "Charging-current control", "entity"),
                _field(c.OPT_EV_ENERGY_REMAINING_ENTITY, "Energy remaining", "entity", domains=("sensor",)),
                _field(c.OPT_EV_POWER_W, "Fixed charging power when current control is unavailable", "number", unit="W", minimum=1, step=1),
                _field(c.OPT_EV_BATTERY_KWH, "Usable vehicle battery capacity", "number", unit="kWh", minimum=0.1, step=0.1),
                _field(c.OPT_EV_CHARGE_EFFICIENCY, "Charging efficiency", "number", unit="%", minimum=1, maximum=100, step=1, scale=100),
                _field(c.OPT_EV_MIN_RUN_SLOTS, "Minimum run", "number", unit="15-minute slots", minimum=1, step=1),
                _field(c.OPT_EV_MIN_CURRENT_A, "Minimum current", "number", unit="A", minimum=0.1, step=0.1),
                _field(c.OPT_EV_MAX_CURRENT_A, "Maximum current", "number", unit="A", minimum=0.1, step=0.1),
                _field(c.OPT_EV_CURRENT_STEP_A, "Current step", "number", unit="A", minimum=0.1, step=0.1),
                _field(c.OPT_EV_PHASE_COUNT, "Charging phases", "number", minimum=1, maximum=3, step=1),
                _field(c.OPT_EV_VOLTAGE, "Charging voltage", "number", unit="V", minimum=1, step=1),
                _field(c.OPT_EV_DEFAULT_DEPARTURE, "Default departure", "time"),
            ],
        },
    ]


def _entry_state(entry: ConfigEntry) -> str:
    return str(getattr(entry.state, "value", entry.state))


def _entry_from_message(
    hass: HomeAssistant, entry_id: str | None
) -> ConfigEntry | None:
    entries = hass.config_entries.async_entries(shs_const.DOMAIN)
    if entry_id:
        entry = hass.config_entries.async_get_entry(entry_id)
        return entry if entry is not None and entry.domain == shs_const.DOMAIN else None
    return entries[0] if len(entries) == 1 else None


def _entries(hass: HomeAssistant) -> list[dict[str, str]]:
    return [
        {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "state": _entry_state(entry),
        }
        for entry in hass.config_entries.async_entries(shs_const.DOMAIN)
    ]


def _entity_catalog(hass: HomeAssistant) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for state in sorted(hass.states.async_all(), key=lambda value: value.entity_id):
        result.append(
            {
                "entity_id": state.entity_id,
                "name": str(state.attributes.get("friendly_name") or state.entity_id),
                "domain": state.entity_id.split(".", 1)[0],
                "state": str(state.state)[:120],
                "unit": state.attributes.get("unit_of_measurement"),
                "device_class": state.attributes.get("device_class"),
            }
        )
    return result


def _mapping_suggestions(
    hass: HomeAssistant,
    device: dict[str, Any],
    control_type: str,
    options: dict[str, Any],
) -> dict[str, Any]:
    """Combine semantic entity suggestions with reviewed legacy values."""
    c = shs_const
    defaults = suggest_device_control_mapping(hass, device, control_type)
    if control_type == "current_limit":
        for target, source in (
            ("current_control_entity_id", c.OPT_EV_CHARGE_CURRENT_ENTITY),
            ("connected_entity_id", c.OPT_EV_CONNECTED_ENTITY),
            ("soc_entity_id", c.OPT_EV_SOC_ENTITY),
            ("target_soc_entity_id", c.OPT_EV_TARGET_SOC_ENTITY),
            ("departure_entity_id", c.OPT_EV_DEPARTURE_ENTITY),
            ("energy_remaining_entity_id", c.OPT_EV_ENERGY_REMAINING_ENTITY),
            ("battery_capacity_kwh", c.OPT_EV_BATTERY_KWH),
            ("min_current_a", c.OPT_EV_MIN_CURRENT_A),
            ("max_current_a", c.OPT_EV_MAX_CURRENT_A),
            ("current_step_a", c.OPT_EV_CURRENT_STEP_A),
            ("phase_count", c.OPT_EV_PHASE_COUNT),
            ("voltage", c.OPT_EV_VOLTAGE),
            ("min_run_slots", c.OPT_EV_MIN_RUN_SLOTS),
        ):
            if options.get(source) not in (None, "", []):
                defaults[target] = options[source]
    elif control_type == "permit_inhibit":
        defaults["max_inhibit_slots"] = options[c.OPT_BOILER_MAX_INHIBIT_SLOTS]
        if options.get(c.OPT_BOILER_POWER_W) not in (None, ""):
            defaults["power_w"] = options[c.OPT_BOILER_POWER_W]
    elif control_type == "switch_schedule":
        defaults["min_run_slots"] = options[c.OPT_POOL_MIN_RUN_SLOTS]
        if options.get(c.OPT_POOL_ENABLED_ENTITY):
            defaults["availability_entity_id"] = options[c.OPT_POOL_ENABLED_ENTITY]
        if options.get(c.OPT_POOL_POWER_W) not in (None, ""):
            defaults["power_w"] = options[c.OPT_POOL_POWER_W]
    return defaults


async def _configuration_payload(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    refresh_roles: bool,
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    portal_error: str | None = None
    try:
        requested = (
            await coordinator.async_refresh_device_configuration()
            if refresh_roles
            else await coordinator.async_cached_device_configuration()
        )
    except (ShsApiError, KeyError, TypeError, ValueError) as err:
        portal_error = str(err)
        requested = await coordinator.async_cached_device_configuration()

    options = resolved_options(hass, dict(entry.options))
    exchange_status = await coordinator.async_cached_exchange_status()
    mappings = options.get(shs_const.OPT_DEVICE_CONTROL_MAPPINGS, {})
    if not isinstance(mappings, dict):
        mappings = {}
    known_entity_ids = {state.entity_id for state in hass.states.async_all()}
    devices: list[dict[str, Any]] = []
    for device in requested:
        control_type = str(device.get("control_type") or "")
        saved = mappings.get(device["key"])
        saved_mapping = (
            dict(saved)
            if isinstance(saved, dict) and saved.get("control_type") == control_type
            else {}
        )
        report = mapping_report(control_type, saved, known_entity_ids)
        devices.append(
            {
                "key": device["key"],
                "name": str(device.get("name") or device["key"]),
                "statistic_id": device.get("statistic_id") or device["key"],
                "category": device.get("category"),
                "load_type": device.get("load_type"),
                "planning_role": device.get("planning_role"),
                "control_type": control_type,
                "mapping": saved_mapping,
                "stale_mapping_control_type": (
                    saved.get("control_type")
                    if isinstance(saved, dict)
                    and saved.get("control_type") != control_type
                    else None
                ),
                "suggested_mapping": _mapping_suggestions(
                    hass, device, control_type, options
                ),
                "fields": list(CONTROL_FIELDS.get(control_type, ())),
                **report,
            }
        )

    ready_devices = [
        device for device in devices if device["mapping_status"] == "ready"
    ]
    setpoint_devices = [
        device for device in devices if device["control_type"] == "setpoint"
    ]
    ready_setpoint_devices = [
        device
        for device in setpoint_devices
        if device["mapping_status"] == "ready"
    ]
    outdoor_entity = options.get(shs_const.OPT_OUTDOOR_TEMPERATURE_ENTITY)
    weather_entity = options.get(shs_const.OPT_WEATHER_FORECAST_ENTITY)
    outdoor_ready = bool(outdoor_entity and outdoor_entity in known_entity_ids)
    forecast_ready = bool(weather_entity and weather_entity in known_entity_ids)
    thermal_slots = int(coordinator.last_thermal_slots_accepted or 0)
    thermal_accepted_until = exchange_status.get("thermal_slots_accepted_until")
    if not setpoint_devices:
        thermal_status = "not_requested"
    elif len(ready_setpoint_devices) != len(setpoint_devices):
        thermal_status = "device_mappings_required"
    elif not outdoor_ready or not forecast_ready:
        thermal_status = "outdoor_sources_required"
    elif thermal_slots == 0 and not thermal_accepted_until:
        thermal_status = "waiting_for_history"
    else:
        thermal_status = "observations_published"

    plan = coordinator.optimisation_plan or {}
    return {
        "entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "state": _entry_state(entry),
        },
        "configuration": options,
        "sections": _configuration_sections(),
        "entities": _entity_catalog(hass),
        "devices": devices,
        "portal": {
            "status": "error" if portal_error else "synchronised",
            "error": portal_error,
            "requested_devices": len(devices),
        },
        "readiness": {
            "planning_mode": options.get(shs_const.OPT_PLANNING_MODE),
            "requested_devices": len(devices),
            "ready_devices": len(ready_devices),
            "device_mapping_gaps": [
                device["name"]
                for device in devices
                if device["mapping_status"] != "ready"
            ],
            "missing_inputs": list(coordinator.optimisation_missing_inputs),
            "last_plan_error": coordinator.last_optimisation_error,
            "last_plan_push": (
                coordinator.last_optimisation_push
                or exchange_status.get("last_optimisation_push")
            ),
            "plan_status": plan.get("status"),
            "plan_model_version": plan.get("model_version"),
            "actual_slots_accepted": coordinator.last_actual_slots_accepted,
            "actuals_accepted_until": coordinator.actuals_accepted_until,
        },
        "thermal": {
            "status": thermal_status,
            "requested_zones": len(setpoint_devices),
            "mapped_zones": len(ready_setpoint_devices),
            "outdoor_temperature_entity": outdoor_entity,
            "outdoor_temperature_ready": outdoor_ready,
            "weather_forecast_entity": weather_entity,
            "weather_forecast_ready": forecast_ready,
            "last_slots_accepted": thermal_slots,
            "accepted_until": thermal_accepted_until,
            "zones": [
                {
                    "key": device["key"],
                    "name": device["name"],
                    "mapping_status": device["mapping_status"],
                    "mapping_error": device["mapping_error"],
                }
                for device in setpoint_devices
            ],
        },
        "diagnostics": {
            "subscription_active": bool((coordinator.data or {}).get("subscription_active")),
            "tariff_status": coordinator.tariff_status,
            "last_tariff_error": coordinator.last_tariff_error,
            "last_daily_push": coordinator.last_push_date,
            "last_daily_push_error": coordinator.last_push_error,
            "last_optimisation_error": coordinator.last_optimisation_error,
            "last_thermal_slots_accepted": thermal_slots,
            "thermal_slots_accepted_until": thermal_accepted_until,
        },
    }


def _allowed_configuration_keys(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    allowed = {
        value
        for name, value in vars(shs_const).items()
        if name.startswith("OPT_") and isinstance(value, str)
    }
    allowed.update(optimisation_defaults(hass))
    allowed.update(
        f"{shs_const.OPT_PREFIX_ENTITIES}{category}"
        for category in shs_const.CONFIGURABLE_CATEGORIES
    )
    allowed.update(entry.options)
    return allowed


def _normalise_field_value(
    hass: HomeAssistant,
    field: dict[str, Any],
    value: Any,
    *,
    context: str,
) -> Any:
    """Validate and normalise one field submitted by the custom panel."""
    kind = field["kind"]
    label = field["label"]
    missing = value in (None, "", [])
    if missing:
        if field.get("required"):
            raise ValueError(f"{context}: {label} is required")
        return None

    if kind == "toggle":
        if not isinstance(value, bool):
            raise ValueError(f"{context}: {label} must be on or off")
        return value

    if kind == "number":
        if isinstance(value, bool):
            raise ValueError(f"{context}: {label} must be a number")
        try:
            number = float(value)
        except (TypeError, ValueError) as err:
            raise ValueError(f"{context}: {label} must be a number") from err
        if not isfinite(number):
            raise ValueError(f"{context}: {label} must be finite")
        displayed = number * float(field.get("scale") or 1)
        if field.get("minimum") is not None and displayed < float(
            field["minimum"]
        ):
            raise ValueError(
                f"{context}: {label} must be at least {field['minimum']}"
            )
        if field.get("maximum") is not None and displayed > float(
            field["maximum"]
        ):
            raise ValueError(
                f"{context}: {label} must be at most {field['maximum']}"
            )
        return number

    if kind == "select":
        text = str(value).strip()
        choices = {choice["value"] for choice in field.get("choices", [])}
        if text not in choices:
            raise ValueError(f"{context}: {label} has an unsupported value")
        return text

    if kind == "time":
        text = str(value).strip()
        match = re.fullmatch(r"(\d{2}):(\d{2})", text)
        if match is None or int(match[1]) > 23 or int(match[2]) > 59:
            raise ValueError(f"{context}: {label} must use HH:MM")
        return text

    if kind in {"entity", "entities"}:
        values = value if isinstance(value, list) else [value]
        if kind == "entities" and not isinstance(value, list):
            raise ValueError(f"{context}: {label} must be a list of entities")
        normalised = []
        for raw_entity_id in values:
            if not isinstance(raw_entity_id, str) or not raw_entity_id.strip():
                raise ValueError(f"{context}: {label} contains an invalid entity")
            entity_id = raw_entity_id.strip()
            state = hass.states.get(entity_id)
            if state is None:
                raise ValueError(f"{context}: {entity_id} does not exist")
            domains = set(field.get("domains", []))
            domain = entity_id.split(".", 1)[0]
            if domains and domain not in domains:
                raise ValueError(
                    f"{context}: {entity_id} is not valid for {label}"
                )
            if entity_id not in normalised:
                normalised.append(entity_id)
        return normalised if kind == "entities" else normalised[0]

    return str(value).strip()


async def async_apply_configuration(
    hass: HomeAssistant,
    entry: ConfigEntry,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Validate and persist one complete or partial panel/service update."""
    unknown = sorted(set(incoming) - _allowed_configuration_keys(hass, entry))
    if unknown:
        raise ValueError("unknown configuration keys: " + ", ".join(unknown))

    options = resolved_options(hass, {**dict(entry.options), **incoming})
    options.pop("setup_method", None)
    for section in _configuration_sections():
        for field in section["fields"]:
            key = field["key"]
            if key not in options:
                if field.get("required"):
                    raise ValueError(f"{section['title']}: {field['label']} is required")
                continue
            value = _normalise_field_value(
                hass,
                field,
                options[key],
                context=section["title"],
            )
            if value is None:
                options.pop(key, None)
            else:
                options[key] = value
    if options.get(shs_const.OPT_PLANNING_MODE) not in {
        shs_const.PLANNING_MODE_DISABLED,
        shs_const.PLANNING_MODE_LIVE,
    }:
        raise ValueError("planning mode must be Off or Live planning")

    mappings = options.get(shs_const.OPT_DEVICE_CONTROL_MAPPINGS, {})
    if not isinstance(mappings, dict):
        raise ValueError("device control mappings must be an object")
    requested = await entry.runtime_data.async_cached_device_configuration()
    known_entity_ids = {state.entity_id for state in hass.states.async_all()}
    for device in requested:
        mapping = mappings.get(device["key"])
        if mapping is None:
            continue
        if not isinstance(mapping, dict):
            raise ValueError(f"{device['name']}: mapping must be an object")
        control_type = str(device.get("control_type") or "")
        if mapping.get("control_type") != control_type:
            continue
        report = mapping_report(control_type, mapping, known_entity_ids)
        if report["mapping_status"] != "ready":
            raise ValueError(
                f"{device['name']}: {report['mapping_error'] or 'mapping is incomplete'}"
            )
        fields = {field["key"]: field for field in CONTROL_FIELDS[control_type]}
        for key, field in fields.items():
            if key not in mapping:
                continue
            value = _normalise_field_value(
                hass,
                field,
                mapping[key],
                context=str(device["name"]),
            )
            if value is None:
                mapping.pop(key, None)
            else:
                mapping[key] = value

    confirmations = (
        (
            shs_const.OPT_POOL_PLANNING_ENABLED,
            shs_const.OPT_POOL_DEFERRABLE_CONFIRMED,
            "pool deferrability",
        ),
        (
            shs_const.OPT_BOILER_PLANNING_ENABLED,
            shs_const.OPT_BOILER_DEFERRABLE_CONFIRMED,
            "water-heater inhibit safety",
        ),
        (
            shs_const.OPT_EV_PLANNING_ENABLED,
            shs_const.OPT_EV_DEFERRABLE_CONFIRMED,
            "EV deferrability",
        ),
        (
            shs_const.OPT_EV_PLANNING_ENABLED,
            shs_const.OPT_EV_ELECTRICAL_CONFIRMED,
            "EV electrical limits",
        ),
    )
    unconfirmed = [
        label
        for enabled_key, confirmation_key, label in confirmations
        if options.get(enabled_key) and not options.get(confirmation_key)
    ]
    if unconfirmed:
        raise ValueError("explicit confirmation required: " + ", ".join(unconfirmed))

    if options.get(shs_const.OPT_PLANNING_MODE) == shs_const.PLANNING_MODE_LIVE:
        options[shs_const.OPT_CONFIGURATION_REVIEWED_AT] = datetime.now(
            timezone.utc
        ).isoformat()
    hass.config_entries.async_update_entry(entry, options=options)
    return options


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{shs_const.DOMAIN}/config/get",
        vol.Optional("config_entry"): str,
        vol.Optional("refresh_roles", default=True): bool,
    }
)
@websocket_api.async_response
async def websocket_get_configuration(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the complete local configuration and current website request."""
    connection.require_admin()
    entry = _entry_from_message(hass, msg.get("config_entry"))
    if entry is None:
        connection.send_result(
            msg["id"],
            {"requires_entry_selection": True, "entries": _entries(hass)},
        )
        return
    if _entry_state(entry) != "loaded":
        connection.send_error(msg["id"], "not_loaded", "The integration is not loaded")
        return
    try:
        payload = await _configuration_payload(
            hass, entry, refresh_roles=bool(msg["refresh_roles"])
        )
    except Exception as err:  # Home Assistant turns this into one visible panel error.
        _LOGGER.exception("Unable to build SHS Energy configuration panel data")
        connection.send_error(msg["id"], "configuration_error", str(err))
        return
    connection.send_result(msg["id"], payload)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{shs_const.DOMAIN}/config/discover",
        vol.Required("config_entry"): str,
    }
)
@websocket_api.async_response
async def websocket_discover_configuration(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Build an Energy Dashboard proposal without saving it."""
    connection.require_admin()
    entry = _entry_from_message(hass, msg["config_entry"])
    if entry is None:
        connection.send_error(msg["id"], "not_found", "SHS Energy entry not found")
        return
    try:
        discovery = await async_discover_configuration(hass, dict(entry.options))
    except Exception as err:
        _LOGGER.exception("SHS Energy automatic discovery failed")
        connection.send_error(msg["id"], "discovery_error", str(err))
        return
    connection.send_result(msg["id"], discovery)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{shs_const.DOMAIN}/config/save",
        vol.Required("config_entry"): str,
        vol.Required("configuration"): dict,
    }
)
@websocket_api.async_response
async def websocket_save_configuration(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Validate and save the panel draft."""
    connection.require_admin()
    entry = _entry_from_message(hass, msg["config_entry"])
    if entry is None:
        connection.send_error(msg["id"], "not_found", "SHS Energy entry not found")
        return
    try:
        options = await async_apply_configuration(
            hass, entry, dict(msg["configuration"])
        )
    except (TypeError, ValueError) as err:
        connection.send_error(msg["id"], "invalid_configuration", str(err))
        return
    connection.send_result(
        msg["id"],
        {
            "saved": True,
            "configuration_reviewed_at": options.get(
                shs_const.OPT_CONFIGURATION_REVIEWED_AT
            ),
        },
    )


async def async_register_config_panel(hass: HomeAssistant) -> None:
    """Register the static bundle, backend commands and cogwheel destination."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(FRONTEND_DIR), False)]
    )
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name=PANEL_ELEMENT,
        module_url=f"{STATIC_URL}/shs-energy-config-panel.js",
        sidebar_title=None,
        sidebar_icon="mdi:home-lightning-bolt",
        config={"domain": shs_const.DOMAIN},
        require_admin=True,
        config_panel_domain=shs_const.DOMAIN,
    )
    websocket_api.async_register_command(hass, websocket_get_configuration)
    websocket_api.async_register_command(hass, websocket_discover_configuration)
    websocket_api.async_register_command(hass, websocket_save_configuration)
