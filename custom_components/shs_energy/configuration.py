"""Small, capability-based configuration for energy planning.

The Energy Dashboard is the canonical meter inventory. Automatic setup copies
only those curated aggregate statistics and discovers live planning inputs by
their semantics. It never selects every phase-level meter or uploads raw state
changes.
"""

from __future__ import annotations

from collections.abc import Iterable
from math import isfinite
import re
from typing import Any

from homeassistant.components.energy import async_get_manager
from homeassistant.core import HomeAssistant, State

from .const import (
    DEFAULT_FORECAST_RESOLUTION_MINUTES,
    DEFAULT_PLANNING_MODE,
    OPT_AUTOMATIC_SETUP,
    OPT_BATTERY_CAPACITY_KWH,
    OPT_BATTERY_CHARGE_EFFICIENCY,
    OPT_BATTERY_CHARGE_MAX_W,
    OPT_BATTERY_DISCHARGE_EFFICIENCY,
    OPT_BATTERY_DISCHARGE_MAX_W,
    OPT_BATTERY_MAX_SOC,
    OPT_BATTERY_MIN_SOC,
    OPT_BATTERY_SOC_ENTITY,
    OPT_BATTERY_TARGET_IS_HARD,
    OPT_BATTERY_TARGET_SOC,
    OPT_BOILER_BASELINE_START,
    OPT_BOILER_DEADLINE,
    OPT_BOILER_MIN_RUN_SLOTS,
    OPT_BOILER_PLANNING_ENABLED,
    OPT_BOILER_POWER_W,
    OPT_ELECTRICITY_PRICE_AREA,
    OPT_EV_BATTERY_KWH,
    OPT_EV_CHARGE_CURRENT_ENTITY,
    OPT_EV_CHARGE_EFFICIENCY,
    OPT_EV_CONNECTED_ENTITY,
    OPT_EV_DEFAULT_DEPARTURE,
    OPT_EV_ENERGY_REMAINING_ENTITY,
    OPT_EV_MIN_RUN_SLOTS,
    OPT_EV_PHASE_COUNT,
    OPT_EV_PLANNING_ENABLED,
    OPT_EV_POWER_W,
    OPT_EV_SOC_ENTITY,
    OPT_EV_TARGET_SOC_ENTITY,
    OPT_EV_VOLTAGE,
    OPT_FORECAST_RESOLUTION_MINUTES,
    OPT_GRID_EXPORT_LIMIT_W,
    OPT_GRID_EXPORT_POWER_ENTITY,
    OPT_GRID_IMPORT_LIMIT_W,
    OPT_PLANNING_MODE,
    OPT_POOL_BASELINE_START,
    OPT_POOL_DEADLINE,
    OPT_POOL_ENABLED_ENTITY,
    OPT_POOL_MIN_RUN_SLOTS,
    OPT_POOL_PLANNING_ENABLED,
    OPT_POOL_POWER_W,
    OPT_PREFIX_ENTITIES,
    OPT_PV_FORECAST_ENTITIES,
    OPT_PV_FORECAST_LATITUDE,
    OPT_PV_FORECAST_LONGITUDE,
    OPT_SUPPLIER_EXPORT_FORECAST_ENTITY,
    OPT_SUPPLIER_EXPORT_PRICE,
    OPT_SUPPLIER_IMPORT_FORECAST_ENTITY,
    OPT_SUPPLIER_IMPORT_PRICE,
    OPT_TERMINAL_ENERGY_VALUE,
    OPT_TERMINAL_SOC_MIN,
)


def optimisation_defaults(hass: HomeAssistant) -> dict[str, Any]:
    """Defaults with product meaning, shared by the UI and runtime."""
    return {
        OPT_PLANNING_MODE: DEFAULT_PLANNING_MODE,
        OPT_AUTOMATIC_SETUP: True,
        OPT_FORECAST_RESOLUTION_MINUTES: DEFAULT_FORECAST_RESOLUTION_MINUTES,
        OPT_PV_FORECAST_LATITUDE: hass.config.latitude,
        OPT_PV_FORECAST_LONGITUDE: hass.config.longitude,
        OPT_BATTERY_MIN_SOC: 0.05,
        OPT_BATTERY_MAX_SOC: 1.0,
        OPT_BATTERY_TARGET_SOC: 0.8,
        # A target is a preference by default. Making 80% hard can force a
        # flexible load onto night import while solar is reserved for storage.
        OPT_BATTERY_TARGET_IS_HARD: False,
        OPT_BATTERY_CHARGE_EFFICIENCY: 0.95,
        OPT_BATTERY_DISCHARGE_EFFICIENCY: 0.95,
        OPT_TERMINAL_SOC_MIN: 0.2,
        OPT_TERMINAL_ENERGY_VALUE: 1.0,
        OPT_POOL_PLANNING_ENABLED: False,
        OPT_POOL_MIN_RUN_SLOTS: 4,
        OPT_POOL_DEADLINE: "20:00",
        OPT_POOL_BASELINE_START: "12:00",
        OPT_BOILER_PLANNING_ENABLED: False,
        OPT_BOILER_MIN_RUN_SLOTS: 4,
        OPT_BOILER_DEADLINE: "22:00",
        OPT_BOILER_BASELINE_START: "06:00",
        OPT_EV_PLANNING_ENABLED: False,
        OPT_EV_CHARGE_EFFICIENCY: 0.92,
        OPT_EV_MIN_RUN_SLOTS: 2,
        OPT_EV_PHASE_COUNT: 3,
        OPT_EV_VOLTAGE: 230.0,
        OPT_EV_DEFAULT_DEPARTURE: "07:00",
    }


def resolved_options(hass: HomeAssistant, options: dict[str, Any]) -> dict[str, Any]:
    """Apply real runtime defaults; selector placeholders are not persisted."""
    return {**optimisation_defaults(hass), **dict(options)}


def _entity_id(statistic_id: Any) -> str | None:
    value = str(statistic_id or "")
    return value if value.startswith(("sensor.", "input_", "binary_sensor.")) else None


def _state_text(state: State) -> str:
    return " ".join(
        (
            state.entity_id.replace("_", " "),
            str(state.attributes.get("friendly_name") or ""),
        )
    ).lower()


def _first_state(
    states: Iterable[State],
    *,
    exact_ids: tuple[str, ...] = (),
    required: tuple[str, ...] = (),
    domain: str | None = None,
) -> State | None:
    by_id = {state.entity_id: state for state in states}
    for entity_id in exact_ids:
        if entity_id in by_id:
            return by_id[entity_id]
    candidates = [
        state
        for state in states
        if (domain is None or state.entity_id.startswith(f"{domain}."))
        and all(token in _state_text(state) for token in required)
    ]
    return sorted(candidates, key=lambda state: (len(state.entity_id), state.entity_id))[0] if candidates else None


def _number(state: State | None) -> float | None:
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    return value if isfinite(value) else None


def _as_watts(state: State | None) -> float | None:
    value = _number(state)
    if value is None:
        return None
    unit = state.attributes.get("unit_of_measurement")
    if unit == "W":
        return value
    if unit == "kW":
        return value * 1_000
    return None


def _as_kwh(state: State | None) -> float | None:
    value = _number(state)
    if value is None:
        return None
    unit = state.attributes.get("unit_of_measurement")
    if unit == "kWh":
        return value
    if unit == "MWh":
        return value * 1_000
    return None


def _category_for_device(name: str) -> str:
    value = name.lower().replace("_", " ")
    rules = (
        ("pool_heating", ("pool", "bassäng")),
        ("hot_water", ("hot water", "boiler", "water heater", "varmvatten")),
        ("ev_charging", ("car charging", "ev charging", "vehicle charger")),
        ("cooling", ("aircon", "air conditioning", "cooling")),
        ("heating", ("heater", "floor heating", "towel rack", "radiator")),
        ("property_energy", ("ftx", "ventilation", "extractor fan")),
    )
    for category, tokens in rules:
        if any(token in value for token in tokens):
            return category
    return "household"


def _price_area(hass: HomeAssistant, states: Iterable[State]) -> str | None:
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            found.update(re.findall(r"\bSE[1-4]\b", value.upper()))
        elif isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, (list, tuple, set)):
            for child in value:
                walk(child)

    for state in states:
        found.update(
            f"SE{match}"
            for match in re.findall(r"nord[_-]?pool[_-]?se([1-4])", state.entity_id)
        )
        walk(state.attributes.get("price_area"))
        walk(state.attributes.get("area"))
        walk(state.attributes.get("region"))
    for domain in ("nordpool", "nord_pool", "tibber"):
        for entry in hass.config_entries.async_entries(domain):
            walk(entry.data)
            walk(entry.options)
    return next(iter(found)) if len(found) == 1 else None


async def async_discover_options(
    hass: HomeAssistant, existing: dict[str, Any]
) -> dict[str, Any]:
    """Build a compact configuration from the Energy Dashboard and live states."""
    options = resolved_options(hass, existing)
    states = list(hass.states.async_all())
    manager = await async_get_manager(hass)
    preferences = manager.data or manager.default_preferences()

    mapped: dict[str, list[str]] = {}
    for source in preferences.get("energy_sources", []):
        source_type = source.get("type")
        if source_type == "grid":
            mapped["grid_import"] = [
                value for flow in source.get("flow_from", [])
                if (value := _entity_id(flow.get("stat_energy_from")))
            ]
            mapped["grid_export"] = [
                value for flow in source.get("flow_to", [])
                if (value := _entity_id(flow.get("stat_energy_to")))
            ]
            import_prices = [
                flow.get("entity_energy_price") for flow in source.get("flow_from", [])
                if flow.get("entity_energy_price")
            ]
            export_prices = [
                flow.get("entity_energy_price") for flow in source.get("flow_to", [])
                if flow.get("entity_energy_price")
            ]
            if import_prices:
                options[OPT_SUPPLIER_IMPORT_PRICE] = import_prices[0]
            if export_prices:
                options[OPT_SUPPLIER_EXPORT_PRICE] = export_prices[0]
        elif source_type == "solar":
            if value := _entity_id(source.get("stat_energy_from")):
                mapped.setdefault("solar_production", []).append(value)
        elif source_type == "battery":
            if value := _entity_id(source.get("stat_energy_to")):
                mapped.setdefault("battery_charge", []).append(value)
            if value := _entity_id(source.get("stat_energy_from")):
                mapped.setdefault("battery_discharge", []).append(value)

    device_categories: dict[str, list[str]] = {}
    for device in preferences.get("device_consumption", []):
        statistic_id = _entity_id(device.get("stat_consumption"))
        if not statistic_id:
            continue
        state = hass.states.get(statistic_id)
        label = str(device.get("name") or "")
        if state is not None:
            label += " " + _state_text(state)
        category = _category_for_device(label or statistic_id)
        device_categories.setdefault(category, []).append(statistic_id)
    mapped.update(device_categories)

    total = _first_state(
        states,
        exact_ids=("sensor.sigen_plant_total_load_consumption",),
        required=("total", "load", "consumption"),
        domain="sensor",
    )
    if total is not None and total.attributes.get("device_class") == "energy":
        mapped["total_consumption"] = [total.entity_id]

    for category, entity_ids in mapped.items():
        options[f"{OPT_PREFIX_ENTITIES}{category}"] = sorted(set(entity_ids))

    pv_forecasts = [
        state.entity_id for state in states
        if state.entity_id.startswith("sensor.")
        and isinstance(state.attributes.get("watts"), dict)
        and state.attributes.get("watts")
    ]
    if pv_forecasts:
        options[OPT_PV_FORECAST_ENTITIES] = sorted(pv_forecasts)

    # SHS grid-price sensors are deliberately not supplier forecasts: using
    # them here would add the network tariff twice in the all-in price. An
    # explicitly selected canonical supplier entity is retained; automatic
    # live setup otherwise uses the Tibber and Nord Pool service adapters.
    for key in (
        OPT_SUPPLIER_IMPORT_FORECAST_ENTITY,
        OPT_SUPPLIER_EXPORT_FORECAST_ENTITY,
    ):
        if str(options.get(key, "")).startswith(
            "sensor.smart_home_solutions_grid_"
        ):
            options.pop(key, None)
    if area := _price_area(hass, states):
        options[OPT_ELECTRICITY_PRICE_AREA] = area

    battery_soc = _first_state(
        states,
        exact_ids=("sensor.sigen_plant_battery_state_of_charge",),
        required=("battery", "state", "charge"),
        domain="sensor",
    )
    if battery_soc:
        options[OPT_BATTERY_SOC_ENTITY] = battery_soc.entity_id
    battery_capacity = _as_kwh(_first_state(
        states,
        exact_ids=("sensor.sigen_plant_rated_energy_capacity",),
        required=("rated", "energy", "capacity"),
        domain="sensor",
    ))
    charge_limit = _as_watts(_first_state(
        states,
        exact_ids=("sensor.sigen_plant_ess_rated_charging_power",),
        required=("rated", "charging", "power"),
        domain="sensor",
    ))
    discharge_limit = _as_watts(_first_state(
        states,
        exact_ids=("sensor.sigen_plant_ess_rated_discharging_power",),
        required=("rated", "discharging", "power"),
        domain="sensor",
    ))
    plant_limit = _as_watts(_first_state(
        states,
        exact_ids=("sensor.sigen_plant_max_active_power",),
        required=("max", "active", "power"),
        domain="sensor",
    ))
    for key, value in (
        (OPT_BATTERY_CAPACITY_KWH, battery_capacity),
        (OPT_BATTERY_CHARGE_MAX_W, charge_limit),
        (OPT_BATTERY_DISCHARGE_MAX_W, discharge_limit),
        (OPT_GRID_IMPORT_LIMIT_W, plant_limit),
        (OPT_GRID_EXPORT_LIMIT_W, plant_limit),
    ):
        if value is not None:
            options[key] = round(value, 3)

    export_power = _first_state(
        states,
        exact_ids=("sensor.sigen_plant_grid_export_power",),
        required=("grid", "export", "power"),
        domain="sensor",
    )
    if export_power:
        options[OPT_GRID_EXPORT_POWER_ENTITY] = export_power.entity_id

    pool_enabled = _first_state(
        states,
        exact_ids=("input_boolean.pool_heating",),
        required=("pool", "heating"),
    )
    if pool_enabled:
        options[OPT_POOL_ENABLED_ENTITY] = pool_enabled.entity_id
    pool_heater_power = _as_watts(_first_state(
        states,
        exact_ids=("sensor.pool_heater_power",),
        required=("pool", "heater", "power"),
        domain="sensor",
    ))
    pool_pump_power = _as_watts(_first_state(
        states,
        exact_ids=("sensor.esphome_pool_pump_power",),
        required=("pool", "pump", "power"),
        domain="sensor",
    ))
    if (
        pool_enabled and pool_enabled.state == "on"
        and pool_heater_power and pool_heater_power > 100
        and pool_pump_power and pool_pump_power > 100
    ):
        options[OPT_POOL_POWER_W] = round(pool_heater_power + pool_pump_power, 1)

    ev_connected = _first_state(
        states,
        exact_ids=("binary_sensor.tesla_model_y_charge_cable",),
        required=("charge", "cable"),
        domain="binary_sensor",
    )
    ev_soc = _first_state(
        states,
        exact_ids=("sensor.tesla_model_y_battery_level",),
        required=("battery", "level"),
        domain="sensor",
    )
    ev_target = _first_state(
        states,
        exact_ids=("number.tesla_model_y_charge_limit",),
        required=("charge", "limit"),
        domain="number",
    )
    ev_current = _first_state(
        states,
        exact_ids=("number.tesla_model_y_charge_current",),
        required=("charge", "current"),
        domain="number",
    )
    ev_remaining = _first_state(
        states,
        exact_ids=("sensor.tesla_model_y_energy_remaining",),
        required=("energy", "remaining"),
        domain="sensor",
    )
    for key, state in (
        (OPT_EV_CONNECTED_ENTITY, ev_connected),
        (OPT_EV_SOC_ENTITY, ev_soc),
        (OPT_EV_TARGET_SOC_ENTITY, ev_target),
        (OPT_EV_CHARGE_CURRENT_ENTITY, ev_current),
        (OPT_EV_ENERGY_REMAINING_ENTITY, ev_remaining),
    ):
        if state:
            options[key] = state.entity_id
    soc = _number(ev_soc)
    remaining = _as_kwh(ev_remaining)
    if soc is not None and remaining is not None and 1 <= soc <= 100:
        options[OPT_EV_BATTERY_KWH] = round(remaining / (soc / 100), 2)

    options[OPT_EV_PLANNING_ENABLED] = bool(
        mapped.get("ev_charging") and ev_connected and ev_soc and ev_target
        and (ev_current or options.get(OPT_EV_POWER_W))
        and options.get(OPT_EV_BATTERY_KWH)
    )
    options[OPT_POOL_PLANNING_ENABLED] = bool(
        mapped.get("pool_heating") and pool_enabled and options.get(OPT_POOL_POWER_W)
    )
    options[OPT_BOILER_PLANNING_ENABLED] = bool(
        mapped.get("hot_water") and options.get(OPT_BOILER_POWER_W)
    )
    options[OPT_AUTOMATIC_SETUP] = True
    return options
