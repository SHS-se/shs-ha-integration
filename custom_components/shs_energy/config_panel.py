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
    area_name_by_id,
    async_discover_configuration,
    entity_area_id,
    entity_area_id_by_id,
    entity_display_name_by_id,
    optimisation_defaults,
    resolved_options,
    suggest_device_control_mapping,
)
from .device_controls import (
    MAPPING_SCHEMA_VERSION,
    MAPPING_SCHEMA_VERSION_FIELD,
    MIGRATED_ROOM_AREA_FIELD,
    is_room_thermal_control,
    mapping_report,
    migrate_device_control_mapping,
)

_LOGGER = logging.getLogger(__name__)

PANEL_URL = "shs-energy"
PANEL_ELEMENT = "shs-energy-config-panel-v3"
STATIC_URL = "/shs_energy_frontend"
FRONTEND_DIR = Path(__file__).parent / "frontend"
FRONTEND_ASSET_VERSION = "0.7.0-beta.8"


def _field(
    key: str,
    label: str,
    kind: str,
    *,
    help_text: str = "",
    domains: tuple[str, ...] = (),
    required: bool = False,
    required_when: str | None = None,
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
    if required_when is not None:
        result["required_when"] = required_when
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


POWER_FIELD = _field(
    "power",
    "Power",
    "power",
    help_text="Choose a power sensor, or enter reviewed watts directly.",
)

def _temperature_field(*, required: bool) -> dict[str, Any]:
    """Describe a mandatory setpoint source or optional room upgrade."""
    return _field(
        "temperature_entity_id",
        "Room or process temperature",
        "entity",
        domains=("sensor", "climate"),
        required=required,
        help_text=(
            "The measured temperature used to learn how this zone responds."
            if required
            else "Optional. Add this to include an existing on/off control in room comfort planning; its saved schedule remains valid without it."
        ),
    )


TEMPERATURE_FIELD = _temperature_field(required=True)
OPTIONAL_TEMPERATURE_FIELD = _temperature_field(required=False)


def _number_control_fields() -> tuple[dict[str, Any], ...]:
    """Describe the shared variable-power number-entity contract."""
    return (
        _field(
            "control_entity_id",
            "Controlled number entity",
            "entity",
            domains=("number", "input_number"),
            required=True,
        ),
        _field(
            "minimum_value",
            "Minimum value",
            "number",
            minimum=0,
            step=0.1,
            required=True,
            help_text="Automatically proposed from Home Assistant when available. A saved value takes precedence.",
        ),
        _field(
            "maximum_value",
            "Maximum value",
            "number",
            minimum=0.1,
            step=0.1,
            required=True,
            help_text="Automatically proposed from Home Assistant when available. A saved value takes precedence.",
        ),
        POWER_FIELD,
    )


CONTROL_FIELDS: dict[str, tuple[dict[str, Any], ...]] = {
    "setpoint": (
        TEMPERATURE_FIELD,
        _field(
            "setpoint_entity_id",
            "Direct setpoint",
            "entity",
            domains=("number", "input_number", "climate"),
            help_text="Use this when one entity contains the current target temperature.",
        ),
        _field(
            "actuator_entity_ids",
            "Controlled heater or climate actuator(s)",
            "entities",
            domains=("switch", "climate", "input_boolean"),
            required=True,
            help_text="These entities reveal actual heating duty. Their Home Assistant area defines the room; this page does not operate them.",
        ),
        _field(
            "companion_actuator_entity_ids",
            "Required companion actuator(s)",
            "entities",
            domains=("switch", "climate", "input_boolean"),
            help_text="For coupled equipment such as a circulation pump.",
        ),
        POWER_FIELD,
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
        POWER_FIELD,
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
        POWER_FIELD,
        _field(
            "min_run_slots",
            "Minimum run",
            "number",
            unit="15-minute slots",
            minimum=1,
            step=1,
        ),
    ),
    "variable_power": _number_control_fields(),
}


def _control_fields(device: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return the control contract, adding room inputs to on/off heaters."""
    control_type = str(device.get("control_type") or "")
    fields = CONTROL_FIELDS.get(control_type, ())
    if (
        is_room_thermal_control(control_type, str(device.get("category") or ""))
        and control_type != "setpoint"
    ):
        return (OPTIONAL_TEMPERATURE_FIELD, *fields)
    return fields


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
            "title": "Solar and electrical measurements",
            "description": "Supplier and price area are configured on the Smart Home Solutions website; prices are fetched and calculated by the service.",
            "fields": [
                _field(c.OPT_PV_FORECAST_ENTITIES, "Solar forecast", "entities", domains=("sensor",)),
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
            "description": "The local switch card owns actuators, power and any minimum run. This section only defines the service obligation.",
            "fields": [
                _field(c.OPT_POOL_PLANNING_ENABLED, "Include pool in planning", "toggle"),
                _field(c.OPT_POOL_DEFERRABLE_CONFIRMED, "I confirm the pool load is deferrable", "toggle"),
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
            ],
        },
        {
            "id": "ev",
            "tab": "storage",
            "title": "EV obligation",
            "description": (
                "Vehicle state supplies the charging need and explicit departure "
                "deadline. Charger current bounds and the optional charging-power "
                "sensor come from its Variable Power device card; the electrical "
                "model is fixed at three 230 V phases."
            ),
            "fields": [
                _field(
                    c.OPT_EV_PLANNING_ENABLED,
                    "Include EV charging in planning",
                    "toggle",
                ),
                _field(
                    c.OPT_EV_DEFERRABLE_CONFIRMED,
                    "I confirm EV charging is deferrable",
                    "toggle",
                ),
                _field(
                    c.OPT_EV_CONNECTED_ENTITY,
                    "Vehicle connected state",
                    "entity",
                    required_when=c.OPT_EV_PLANNING_ENABLED,
                ),
                _field(
                    c.OPT_EV_SOC_ENTITY,
                    "Vehicle battery SOC",
                    "entity",
                    domains=("sensor",),
                    required_when=c.OPT_EV_PLANNING_ENABLED,
                ),
                _field(
                    c.OPT_EV_TARGET_SOC_ENTITY,
                    "Vehicle target SOC",
                    "entity",
                    required_when=c.OPT_EV_PLANNING_ENABLED,
                ),
                _field(
                    c.OPT_EV_DEPARTURE_ENTITY,
                    "Departure timestamp",
                    "entity",
                    required_when=c.OPT_EV_PLANNING_ENABLED,
                    help_text=(
                        "The entity must contain a timezone-aware timestamp. "
                        "The planner will not invent a departure deadline."
                    ),
                ),
                _field(
                    c.OPT_EV_ENERGY_REMAINING_ENTITY,
                    "Usable energy remaining",
                    "entity",
                    domains=("sensor",),
                    required_when=c.OPT_EV_PLANNING_ENABLED,
                    help_text=(
                        "Used with current SOC to derive usable battery capacity "
                        "automatically."
                    ),
                ),
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
    area_names = area_name_by_id(hass)
    for state in sorted(hass.states.async_all(), key=lambda value: value.entity_id):
        area_id = entity_area_id(hass, state.entity_id)
        result.append(
            {
                "entity_id": state.entity_id,
                "name": str(state.attributes.get("friendly_name") or state.entity_id),
                "domain": state.entity_id.split(".", 1)[0],
                "state": str(state.state)[:120],
                "unit": state.attributes.get("unit_of_measurement"),
                "device_class": state.attributes.get("device_class"),
                "minimum": state.attributes.get("min"),
                "maximum": state.attributes.get("max"),
                "area_id": area_id,
                "area_name": area_names.get(area_id) if area_id else None,
            }
        )
    return result


def _mapping_suggestions(
    hass: HomeAssistant,
    device: dict[str, Any],
    control_type: str,
) -> dict[str, Any]:
    """Return live Home Assistant suggestions for the current card contract."""
    return suggest_device_control_mapping(hass, device, control_type)


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
    entity_names = entity_display_name_by_id(hass)
    area_names = area_name_by_id(hass)
    entity_area_ids = entity_area_id_by_id(hass)
    devices: list[dict[str, Any]] = []
    for device in requested:
        control_type = str(device.get("control_type") or "")
        saved = mappings.get(device["key"])
        saved_mapping = (
            dict(saved)
            if isinstance(saved, dict) and saved.get("control_type") == control_type
            else {}
        )
        report = mapping_report(
            control_type,
            saved,
            known_entity_ids,
            entity_names,
            area_names,
            entity_area_ids,
            room_control=is_room_thermal_control(
                control_type, str(device.get("category") or "")
            ),
        )
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
                    hass, device, control_type
                ),
                "fields": list(_control_fields(device)),
                **report,
            }
        )

    ready_devices = [
        device for device in devices if device["mapping_status"] == "ready"
    ]
    thermal_devices = [
        device
        for device in devices
        if is_room_thermal_control(device["control_type"], device.get("category"))
        and (
            device["control_type"] == "setpoint"
            or bool(device.get("mapping", {}).get("temperature_entity_id"))
        )
    ]
    ready_thermal_devices = [
        device
        for device in thermal_devices
        if device["mapping_status"] == "ready"
        and isinstance(device.get("mapping_summary", {}).get("room_key"), str)
    ]
    mapped_room_keys = {
        device["mapping_summary"].get("room_key")
        for device in ready_thermal_devices
        if isinstance(device.get("mapping_summary"), dict)
        and isinstance(device["mapping_summary"].get("room_key"), str)
    }
    outdoor_entity = options.get(shs_const.OPT_OUTDOOR_TEMPERATURE_ENTITY)
    weather_entity = options.get(shs_const.OPT_WEATHER_FORECAST_ENTITY)
    outdoor_ready = bool(outdoor_entity and outdoor_entity in known_entity_ids)
    forecast_ready = bool(weather_entity and weather_entity in known_entity_ids)
    thermal_slots = int(coordinator.last_thermal_slots_accepted or 0)
    thermal_accepted_until = exchange_status.get("thermal_slots_accepted_until")
    if not thermal_devices:
        thermal_status = "not_requested"
    elif len(ready_thermal_devices) != len(thermal_devices):
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
            "requested_zones": len(thermal_devices),
            "mapped_zones": len(ready_thermal_devices),
            "mapped_rooms": len(mapped_room_keys),
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
                    "room_name": device["mapping_summary"].get("room_name")
                    if isinstance(device.get("mapping_summary"), dict)
                    else None,
                    "mapping_status": device["mapping_status"],
                    "mapping_error": device["mapping_error"],
                }
                for device in thermal_devices
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

    if kind == "power":
        text = str(value).strip()
        state = hass.states.get(text)
        if state is not None:
            unit = state.attributes.get("unit_of_measurement")
            if not text.startswith("sensor.") or unit not in {"W", "kW"}:
                raise ValueError(
                    f"{context}: {label} must be a W or kW power sensor"
                )
            return text
        try:
            watts = float(text)
        except (TypeError, ValueError) as err:
            raise ValueError(
                f"{context}: {label} must be a power entity or watts"
            ) from err
        if not isfinite(watts) or watts <= 0:
            raise ValueError(f"{context}: {label} must be positive watts")
        return watts

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


def _normalise_device_mapping(
    hass: HomeAssistant,
    device: dict[str, Any],
    mapping: dict[str, Any],
    existing_mapping: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one mapping while retaining lossless migration data."""
    control_type = str(device.get("control_type") or "")
    if control_type not in CONTROL_FIELDS:
        raise ValueError(f"{device['name']}: unsupported control type")

    entity_limits = {
        state.entity_id: (
            state.attributes.get("min"),
            state.attributes.get("max"),
        )
        for state in hass.states.async_all()
    }
    submitted, _changed = migrate_device_control_mapping(
        mapping,
        entity_limits=entity_limits,
        recover_room=False,
    )
    if submitted.get("control_type") != control_type:
        raise ValueError(
            f"{device['name']}: mapping belongs to a different control type"
        )
    normalised = dict(submitted)
    normalised["control_type"] = control_type
    normalised[MAPPING_SCHEMA_VERSION_FIELD] = MAPPING_SCHEMA_VERSION
    normalised.pop(MIGRATED_ROOM_AREA_FIELD, None)
    migrated_room_area = (existing_mapping or {}).get(MIGRATED_ROOM_AREA_FIELD)
    if isinstance(migrated_room_area, str) and migrated_room_area.strip():
        normalised[MIGRATED_ROOM_AREA_FIELD] = migrated_room_area

    fields = _control_fields(device)
    for field in fields:
        normalised.pop(field["key"], None)
    for field in fields:
        key = field["key"]
        if key not in submitted:
            continue
        value = _normalise_field_value(
            hass,
            field,
            submitted[key],
            context=str(device["name"]),
        )
        if value is not None:
            normalised[key] = value

    report = mapping_report(
        control_type,
        normalised,
        {state.entity_id for state in hass.states.async_all()},
        entity_display_name_by_id(hass),
        area_name_by_id(hass),
        entity_area_id_by_id(hass),
        room_control=is_room_thermal_control(
            control_type, str(device.get("category") or "")
        ),
    )
    if report["mapping_status"] != "ready":
        raise ValueError(
            f"{device['name']}: {report['mapping_error'] or 'mapping is incomplete'}"
        )
    return normalised


async def async_apply_configuration(
    hass: HomeAssistant,
    entry: ConfigEntry,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Validate and persist a complete or partial non-device update."""
    if shs_const.OPT_DEVICE_CONTROL_MAPPINGS in incoming:
        raise ValueError("device mappings must be saved from their own card")
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
    conditionally_missing = [
        f"{section['title']}: {field['label']} is required"
        for section in _configuration_sections()
        for field in section["fields"]
        if field.get("required_when")
        and options.get(field["required_when"])
        and options.get(field["key"]) in (None, "", [])
    ]
    if conditionally_missing:
        raise ValueError("; ".join(conditionally_missing))
    if options.get(shs_const.OPT_PLANNING_MODE) not in {
        shs_const.PLANNING_MODE_DISABLED,
        shs_const.PLANNING_MODE_LIVE,
    }:
        raise ValueError("planning mode must be Off or Live planning")

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


async def async_apply_device_mapping(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_key: str,
    incoming: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate, report and save one device mapping without touching its peers."""
    requested = await entry.runtime_data.async_cached_device_configuration()
    device = next((item for item in requested if item["key"] == device_key), None)
    if device is None:
        raise ValueError("the website no longer requests this controllable device")

    options = dict(entry.options)
    existing = options.get(shs_const.OPT_DEVICE_CONTROL_MAPPINGS, {})
    mappings = {
        key: dict(value)
        for key, value in existing.items()
        if isinstance(key, str) and isinstance(value, dict)
    } if isinstance(existing, dict) else {}
    if incoming is None:
        mappings.pop(device_key, None)
    else:
        mappings[device_key] = _normalise_device_mapping(
            hass,
            device,
            incoming,
            mappings.get(device_key),
        )

    report = await entry.runtime_data.async_report_device_mapping(
        device_key, mappings
    )
    options[shs_const.OPT_DEVICE_CONTROL_MAPPINGS] = mappings
    if options.get(shs_const.OPT_PLANNING_MODE) == shs_const.PLANNING_MODE_LIVE:
        options[shs_const.OPT_CONFIGURATION_REVIEWED_AT] = datetime.now(
            timezone.utc
        ).isoformat()
    hass.config_entries.async_update_entry(entry, options=options)
    await entry.runtime_data.async_optimisation_push(force_plan=True)
    return report


@websocket_api.require_admin
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


@websocket_api.require_admin
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


@websocket_api.require_admin
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


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{shs_const.DOMAIN}/config/save_device",
        vol.Required("config_entry"): str,
        vol.Required("device_key"): str,
        vol.Required("mapping"): vol.Any(dict, None),
    }
)
@websocket_api.async_response
async def websocket_save_device_configuration(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Save and report one local control mapping."""
    entry = _entry_from_message(hass, msg["config_entry"])
    if entry is None:
        connection.send_error(msg["id"], "not_found", "SHS Energy entry not found")
        return
    try:
        report = await async_apply_device_mapping(
            hass,
            entry,
            msg["device_key"],
            dict(msg["mapping"]) if msg["mapping"] is not None else None,
        )
        panel = await _configuration_payload(hass, entry, refresh_roles=False)
    except (ShsApiError, TypeError, ValueError) as err:
        connection.send_error(msg["id"], "invalid_device_mapping", str(err))
        return
    connection.send_result(msg["id"], {"saved": True, **report, "panel": panel})


async def async_register_config_panel(hass: HomeAssistant) -> None:
    """Register the static bundle, backend commands and cogwheel destination."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(FRONTEND_DIR), False)]
    )
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name=PANEL_ELEMENT,
        module_url=(
            f"{STATIC_URL}/shs-energy-config-panel.js"
            f"?v={FRONTEND_ASSET_VERSION}"
        ),
        sidebar_title=None,
        sidebar_icon="mdi:home-lightning-bolt",
        config={"domain": shs_const.DOMAIN},
        require_admin=True,
        config_panel_domain=shs_const.DOMAIN,
    )
    websocket_api.async_register_command(hass, websocket_get_configuration)
    websocket_api.async_register_command(hass, websocket_discover_configuration)
    websocket_api.async_register_command(hass, websocket_save_configuration)
    websocket_api.async_register_command(hass, websocket_save_device_configuration)
