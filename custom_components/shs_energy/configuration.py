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
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONFIGURABLE_CATEGORIES,
    DEFAULT_FORECAST_RESOLUTION_MINUTES,
    DEFAULT_PLANNING_MODE,
    OPT_AUTOMATIC_SETUP,
    OPT_BATTERY_CAPACITY_KWH,
    OPT_BATTERY_CHARGE_EFFICIENCY,
    OPT_BATTERY_CHARGE_MAX_W,
    OPT_BATTERY_DISCHARGE_EFFICIENCY,
    OPT_BATTERY_EXPORT_ENABLED,
    OPT_BATTERY_EXPORT_MIN_PRICE,
    OPT_BATTERY_EXPORT_RESERVE_SOC,
    OPT_BATTERY_DISCHARGE_MAX_W,
    OPT_BATTERY_MAX_SOC,
    OPT_BATTERY_MIN_SOC,
    OPT_BATTERY_SOC_ENTITY,
    OPT_BATTERY_TARGET_IS_HARD,
    OPT_BATTERY_TARGET_SOC,
    OPT_BOILER_DEFERRABLE_CONFIRMED,
    OPT_BOILER_PLANNING_ENABLED,
    OPT_DISCOVERY_EVIDENCE,
    OPT_DEVICE_CONTROL_MAPPINGS,
    OPT_OUTDOOR_TEMPERATURE_ENTITY,
    OPT_WEATHER_FORECAST_ENTITY,
    OPT_EV_BATTERY_KWH,
    OPT_EV_CHARGE_EFFICIENCY,
    OPT_EV_CONNECTED_ENTITY,
    OPT_EV_DEFAULT_DEPARTURE,
    OPT_EV_DEFERRABLE_CONFIRMED,
    OPT_EV_ELECTRICAL_CONFIRMED,
    OPT_EV_ENERGY_REMAINING_ENTITY,
    OPT_EV_MIN_RUN_SLOTS,
    OPT_EV_PHASE_COUNT,
    OPT_EV_PLANNING_ENABLED,
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
    OPT_POOL_DEFERRABLE_CONFIRMED,
    OPT_POOL_PLANNING_ENABLED,
    OPT_PREFIX_ENTITIES,
    OPT_PV_FORECAST_ENTITIES,
    OPT_PV_FORECAST_LATITUDE,
    OPT_PV_FORECAST_LONGITUDE,
    OPT_TERMINAL_ENERGY_VALUE,
    OPT_TERMINAL_SOC_MIN,
)
from .optimisation import suggested_device_planning, suggested_load_type


def optimisation_defaults(hass: HomeAssistant) -> dict[str, Any]:
    """Defaults with product meaning, shared by the UI and runtime."""
    return {
        OPT_PLANNING_MODE: DEFAULT_PLANNING_MODE,
        OPT_AUTOMATIC_SETUP: True,
        OPT_DEVICE_CONTROL_MAPPINGS: {},
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
        # Export from storage is an explicit customer preference. It is
        # advisory until a separately reviewed battery executor exists.
        OPT_BATTERY_EXPORT_ENABLED: False,
        OPT_BATTERY_EXPORT_RESERVE_SOC: 0.8,
        OPT_BATTERY_EXPORT_MIN_PRICE: 2.5,
        OPT_TERMINAL_SOC_MIN: 0.2,
        OPT_TERMINAL_ENERGY_VALUE: 1.0,
        OPT_POOL_PLANNING_ENABLED: False,
        OPT_POOL_DEFERRABLE_CONFIRMED: False,
        OPT_POOL_DEADLINE: "20:00",
        OPT_POOL_BASELINE_START: "12:00",
        OPT_BOILER_PLANNING_ENABLED: False,
        OPT_BOILER_DEFERRABLE_CONFIRMED: False,
        OPT_EV_PLANNING_ENABLED: False,
        OPT_EV_DEFERRABLE_CONFIRMED: False,
        OPT_EV_ELECTRICAL_CONFIRMED: False,
        OPT_EV_CHARGE_EFFICIENCY: 0.92,
        OPT_EV_MIN_RUN_SLOTS: 2,
        OPT_EV_PHASE_COUNT: 3,
        OPT_EV_VOLTAGE: 230.0,
        OPT_EV_DEFAULT_DEPARTURE: "07:00",
    }


def resolved_options(hass: HomeAssistant, options: dict[str, Any]) -> dict[str, Any]:
    """Apply real runtime defaults; selector placeholders are not persisted."""
    return {**optimisation_defaults(hass), **dict(options)}


def area_name_by_id(hass: HomeAssistant) -> dict[str, str]:
    """Return Home Assistant's stable area ids and user-facing room names."""
    registry = ar.async_get(hass)
    return {area.id: area.name for area in registry.async_list_areas()}


def entity_area_id(hass: HomeAssistant, entity_id: str) -> str | None:
    """Resolve an entity's own area, then its device's area."""
    entity = er.async_get(hass).async_get(entity_id)
    if entity is None:
        return None
    if entity.area_id:
        return entity.area_id
    if not entity.device_id:
        return None
    device = dr.async_get(hass).async_get(entity.device_id)
    return device.area_id if device is not None else None


def entity_display_name_by_id(hass: HomeAssistant) -> dict[str, str]:
    """Name controlled hardware without exposing entity ids to the website."""
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    result: dict[str, str] = {}
    for state in hass.states.async_all():
        entity = entities.async_get(state.entity_id)
        device = (
            devices.async_get(entity.device_id)
            if entity is not None and entity.device_id
            else None
        )
        result[state.entity_id] = str(
            (device.name_by_user if device else None)
            or (device.name if device else None)
            or state.attributes.get("friendly_name")
            or state.entity_id
        )
    return result


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


def _attribute_number(state: State | None, key: str) -> float | None:
    if state is None:
        return None
    try:
        value = float(state.attributes.get(key))
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
    if "tesla" in value and any(
        token in value for token in ("charg", "energy added")
    ):
        return "ev_charging"
    rules = (
        ("pool_heating", ("pool", "bassäng")),
        (
            "hot_water",
            (
                "hot water",
                "boiler",
                "water heater",
                "varmvatten",
                "varmvattenberedare",
                "vvb",
            ),
        ),
        (
            "ev_charging",
            (
                "car charging",
                "ev charging",
                "vehicle charger",
                "wallbox",
                "energy added",
            ),
        ),
        ("cooling", ("aircon", "air conditioning", "cooling")),
        ("heating", ("heater", "floor heating", "towel rack", "radiator")),
        ("property_energy", ("ftx", "ventilation", "extractor fan")),
    )
    for category, tokens in rules:
        if any(token in value for token in tokens):
            return category
    return "household"


def _energy_dashboard_inventory(
    hass: HomeAssistant, preferences: dict[str, Any]
) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    seen: set[str] = set()
    for device in preferences.get("device_consumption", []):
        statistic_id = str(device.get("stat_consumption") or "").strip()
        if not statistic_id or statistic_id in seen:
            continue
        seen.add(statistic_id)
        state = hass.states.get(statistic_id)
        configured_name = str(device.get("name") or "").strip()
        state_name = (
            str(state.attributes.get("friendly_name") or "").strip()
            if state is not None else ""
        )
        # The registry/state name is live Home Assistant metadata. Energy
        # Dashboard preferences may retain an older copied label after a user
        # rename, so the live friendly name takes precedence on every sync.
        name = state_name or configured_name or statistic_id
        evidence_text = " ".join(
            value for value in (name, statistic_id, _state_text(state) if state else "")
            if value
        )
        category = _category_for_device(evidence_text)
        load_type, inference = suggested_load_type(evidence_text, category)
        planning_role, control_type, planning_inference = (
            suggested_device_planning(category, load_type)
        )
        inventory.append({
            "key": statistic_id,
            "statistic_id": statistic_id,
            "name": name,
            "category": category,
            "suggested_load_type": load_type,
            "suggested_planning_role": planning_role,
            "suggested_control_type": control_type,
            "active_power_w": None,
            "profile_sample_count": 0,
            "inference": {
                **inference,
                "planning": planning_inference,
            },
        })
    return inventory


async def async_energy_dashboard_inventory(
    hass: HomeAssistant,
) -> list[dict[str, Any]]:
    """Read the current Energy Dashboard device inventory."""
    manager = await async_get_manager(hass)
    preferences = manager.data or manager.default_preferences()
    return _energy_dashboard_inventory(hass, preferences)


_CONTROL_TOKEN_STOP_WORDS = {
    "energy", "sensor", "power", "load", "heating", "heater", "cooling",
    "consumption", "device", "room", "the", "and",
}


def _control_tokens(*values: Any) -> set[str]:
    text = " ".join(str(value or "") for value in values).lower().replace("'s", "s")
    return {
        token for token in re.findall(r"[a-zåäö0-9]+", text)
        if len(token) > 1 and token not in _CONTROL_TOKEN_STOP_WORDS
    }


def _suggest_control_entities(
    hass: HomeAssistant,
    device: dict[str, Any],
    *,
    domains: tuple[str, ...],
    field_tokens: tuple[str, ...],
    units: tuple[str, ...] = (),
    multiple: bool = False,
) -> str | list[str] | None:
    """Rank local entities semantically; suggestions always require review."""
    device_tokens = _control_tokens(
        device.get("name"), device.get("statistic_id"), device.get("category")
    )
    ranked: list[tuple[int, str]] = []
    for state in hass.states.async_all():
        domain = state.entity_id.split(".", 1)[0]
        if domain not in domains:
            continue
        text = _state_text(state)
        if any(token in text for token in ("child lock", "lock state", "configuration")):
            continue
        tokens = _control_tokens(text)
        overlap = len(device_tokens & tokens)
        if overlap == 0:
            continue
        field_hits = sum(token in text for token in field_tokens)
        unit_hit = state.attributes.get("unit_of_measurement") in units
        if field_hits == 0 and not unit_hit:
            continue
        score = overlap * 10 + field_hits * 3 + (2 if unit_hit else 0)
        ranked.append((score, state.entity_id))
    ranked.sort(key=lambda value: (-value[0], len(value[1]), value[1]))
    if not ranked:
        return [] if multiple else None
    if not multiple:
        return ranked[0][1]
    best = ranked[0][0]
    return [entity_id for score, entity_id in ranked if score >= best - 2][:6]


def suggest_device_control_mapping(
    hass: HomeAssistant,
    device: dict[str, Any],
    control_type: str,
) -> dict[str, Any]:
    """Propose installation-local fields without silently accepting them."""
    mapping: dict[str, Any] = {"control_type": control_type}

    def set_if_found(key: str, value: Any) -> None:
        if value not in (None, "", []):
            mapping[key] = value

    set_if_found("power", _suggest_control_entities(
        hass, device, domains=("sensor",), field_tokens=("power", "effekt"),
        units=("W", "kW"),
    ))
    if control_type == "setpoint":
        set_if_found("temperature_entity_id", _suggest_control_entities(
            hass, device, domains=("sensor", "climate"),
            field_tokens=("temperature", "temp", "temperatur"), units=("°C",),
        ))
        set_if_found("setpoint_entity_id", _suggest_control_entities(
            hass, device, domains=("number", "input_number", "climate"),
            field_tokens=("setpoint", "target temp", "target temperature", "börvärde"),
        ))
        set_if_found("actuator_entity_ids", _suggest_control_entities(
            hass, device, domains=("switch", "climate"),
            field_tokens=("heater", "heating", "aircon", "climate", "värme"),
            multiple=True,
        ))
        room_sources = [
            mapping.get("temperature_entity_id"),
            *(mapping.get("actuator_entity_ids") or []),
            device.get("statistic_id"),
        ]
        for source in room_sources:
            if not isinstance(source, str):
                continue
            if area_id := entity_area_id(hass, source):
                mapping["area_id"] = area_id
                break
    elif control_type in ("switch_schedule", "permit_inhibit"):
        set_if_found("actuator_entity_ids", _suggest_control_entities(
            hass, device, domains=("switch", "input_boolean", "climate"),
            field_tokens=("heater", "heating", "boiler", "pump", "switch", "värme"),
            multiple=True,
        ))
    elif control_type == "variable_power":
        current_control = device.get("category") == "ev_charging"
        control_entity = _suggest_control_entities(
            hass,
            device,
            domains=("number", "input_number"),
            field_tokens=(
                ("charge current", "charging current", "ström")
                if current_control
                else ("power", "output", "limit", "effekt")
            ),
            units=("A",) if current_control else ("W", "kW"),
        )
        set_if_found("control_entity_id", control_entity)
        if isinstance(control_entity, str):
            state = hass.states.get(control_entity)
            if state is not None:
                set_if_found("minimum_value", _attribute_number(state, "min"))
                set_if_found("maximum_value", _attribute_number(state, "max"))
    return mapping


async def async_discover_configuration(
    hass: HomeAssistant, existing: dict[str, Any]
) -> dict[str, Any]:
    """Return a reviewable proposal without deciding what is deferrable."""
    options = resolved_options(hass, existing)
    evidence: dict[str, dict[str, Any]] = {}

    def record(
        key: str,
        value: Any,
        *,
        source: str,
        confidence: str,
        detail: str,
    ) -> None:
        options[key] = value
        evidence[key] = {
            "source": source,
            "confidence": confidence,
            "detail": detail,
        }

    def record_state(
        key: str,
        state: State | None,
        *,
        exact_ids: tuple[str, ...],
        detail: str,
    ) -> None:
        if state is None:
            return
        exact = state.entity_id in exact_ids
        record(
            key,
            state.entity_id,
            source="exact_entity_id" if exact else "entity_name_match",
            confidence="high" if exact else "medium",
            detail=detail,
        )

    states = list(hass.states.async_all())
    manager = await async_get_manager(hass)
    preferences = manager.data or manager.default_preferences()
    mapped: dict[str, list[str]] = {}
    for source in preferences.get("energy_sources", []):
        source_type = source.get("type")
        if source_type == "grid":
            # Home Assistant has represented a grid source both with direct
            # ``stat_energy_*`` fields and with per-flow lists. They describe
            # the same curated Energy Dashboard contract.
            grid_import = [
                value
                for candidate in (source.get("stat_energy_from"),)
                if (value := _entity_id(candidate))
            ] + [
                value
                for flow in source.get("flow_from", [])
                if (value := _entity_id(flow.get("stat_energy_from")))
            ]
            grid_export = [
                value
                for candidate in (source.get("stat_energy_to"),)
                if (value := _entity_id(candidate))
            ] + [
                value
                for flow in source.get("flow_to", [])
                if (value := _entity_id(flow.get("stat_energy_to")))
            ]
            mapped["grid_import"] = list(dict.fromkeys(grid_import))
            mapped["grid_export"] = list(dict.fromkeys(grid_export))
        elif source_type == "solar":
            if value := _entity_id(source.get("stat_energy_from")):
                mapped.setdefault("solar_production", []).append(value)
        elif source_type == "battery":
            if value := _entity_id(source.get("stat_energy_to")):
                mapped.setdefault("battery_charge", []).append(value)
            if value := _entity_id(source.get("stat_energy_from")):
                mapped.setdefault("battery_discharge", []).append(value)

    device_categories: dict[str, list[str]] = {}
    for device in _energy_dashboard_inventory(hass, preferences):
        device_categories.setdefault(device["category"], []).append(
            device["statistic_id"]
        )
    mapped.update(device_categories)

    total_ids = ("sensor.sigen_plant_total_load_consumption",)
    total = _first_state(
        states,
        exact_ids=total_ids,
        required=("total", "load", "consumption"),
        domain="sensor",
    )
    if total is not None and total.attributes.get("device_class") == "energy":
        mapped["total_consumption"] = [total.entity_id]

    for category in CONFIGURABLE_CATEGORIES:
        key = f"{OPT_PREFIX_ENTITIES}{category}"
        discovered_total = (
            category == "total_consumption"
            and total is not None
            and mapped.get(category) == [total.entity_id]
        )
        record(
            key,
            sorted(set(mapped.get(category, []))),
            source=(
                "whole_home_entity_contract"
                if discovered_total
                else "energy_dashboard"
            ),
            confidence="high" if discovered_total else "authoritative",
            detail=(
                "Whole-home energy entity matched by device class and semantics"
                if discovered_total
                else "Aggregate statistic selected in Home Assistant Energy"
            ),
        )
    pv_forecasts = sorted(
        state.entity_id
        for state in states
        if state.entity_id.startswith("sensor.")
        and isinstance(state.attributes.get("watts"), dict)
        and state.attributes.get("watts")
    )
    if pv_forecasts:
        record(
            OPT_PV_FORECAST_ENTITIES,
            pv_forecasts,
            source="timestamped_watts_contract",
            confidence="high",
            detail="Entity exposes timestamped watts rather than positional samples",
        )

    battery_soc_ids = ("sensor.sigen_plant_battery_state_of_charge",)
    battery_capacity_ids = ("sensor.sigen_plant_rated_energy_capacity",)
    charge_limit_ids = ("sensor.sigen_plant_ess_rated_charging_power",)
    discharge_limit_ids = ("sensor.sigen_plant_ess_rated_discharging_power",)
    plant_limit_ids = ("sensor.sigen_plant_max_active_power",)
    battery_soc = _first_state(
        states,
        exact_ids=battery_soc_ids,
        required=("battery", "state", "charge"),
        domain="sensor",
    )
    record_state(
        OPT_BATTERY_SOC_ENTITY,
        battery_soc,
        exact_ids=battery_soc_ids,
        detail="Live battery state of charge",
    )
    equipment_values = (
        (
            OPT_BATTERY_CAPACITY_KWH,
            _first_state(
                states,
                exact_ids=battery_capacity_ids,
                required=("rated", "energy", "capacity"),
                domain="sensor",
            ),
            _as_kwh,
            battery_capacity_ids,
        ),
        (
            OPT_BATTERY_CHARGE_MAX_W,
            _first_state(
                states,
                exact_ids=charge_limit_ids,
                required=("rated", "charging", "power"),
                domain="sensor",
            ),
            _as_watts,
            charge_limit_ids,
        ),
        (
            OPT_BATTERY_DISCHARGE_MAX_W,
            _first_state(
                states,
                exact_ids=discharge_limit_ids,
                required=("rated", "discharging", "power"),
                domain="sensor",
            ),
            _as_watts,
            discharge_limit_ids,
        ),
        (
            OPT_GRID_IMPORT_LIMIT_W,
            _first_state(
                states,
                exact_ids=plant_limit_ids,
                required=("max", "active", "power"),
                domain="sensor",
            ),
            _as_watts,
            plant_limit_ids,
        ),
    )
    for key, state, converter, exact_ids in equipment_values:
        value = converter(state)
        if value is None or state is None:
            continue
        exact = state.entity_id in exact_ids
        record(
            key,
            round(value, 3),
            source="equipment_rating_entity",
            confidence="high" if exact else "medium",
            detail=f"Declared by {state.entity_id}",
        )
    if options.get(OPT_GRID_IMPORT_LIMIT_W):
        record(
            OPT_GRID_EXPORT_LIMIT_W,
            options[OPT_GRID_IMPORT_LIMIT_W],
            source="equipment_rating_entity",
            confidence="medium",
            detail="Proposed from the same plant limit; review if export is restricted",
        )

    export_power_ids = ("sensor.sigen_plant_grid_export_power",)
    export_power = _first_state(
        states,
        exact_ids=export_power_ids,
        required=("grid", "export", "power"),
        domain="sensor",
    )
    record_state(
        OPT_GRID_EXPORT_POWER_ENTITY,
        export_power,
        exact_ids=export_power_ids,
        detail="Live measured export available to reactive control",
    )

    # Outdoor temperature drives every zone's heat loss, so it is discovered
    # once for the home rather than per device. A local sensor is preferred
    # over the weather provider's regional value; the provider entity is kept
    # separately because only it carries a forecast.
    outdoor_ids = ("sensor.outdoor_temperature",)
    outdoor = _first_state(
        states,
        exact_ids=outdoor_ids,
        required=("outdoor", "temperature"),
        domain="sensor",
    ) or _first_state(
        states,
        exact_ids=(),
        required=("outside", "temperature"),
        domain="sensor",
    )
    if outdoor is not None and outdoor.attributes.get("unit_of_measurement") not in (
        "°C",
        "°F",
    ):
        # A temperature entity without a temperature unit is some other
        # quantity that merely reads like one. Guessing would silently poison
        # every zone fit that consumes it.
        outdoor = None
    record_state(
        OPT_OUTDOOR_TEMPERATURE_ENTITY,
        outdoor,
        exact_ids=outdoor_ids,
        detail="Measured outdoor air temperature for zone heat-loss fitting",
    )

    forecast_ids = ("weather.forecast_home", "weather.home")
    forecast = _first_state(states, exact_ids=forecast_ids, required=(), domain="weather")
    record_state(
        OPT_WEATHER_FORECAST_ENTITY,
        forecast,
        exact_ids=forecast_ids,
        detail="Weather entity providing the outdoor temperature forecast",
    )

    ev_current = _first_state(
        states,
        exact_ids=("number.tesla_model_y_charge_current",),
        required=("charge", "current"),
        domain="number",
    )
    ev_matches = (
        (
            OPT_EV_CONNECTED_ENTITY,
            _first_state(
                states,
                exact_ids=("binary_sensor.tesla_model_y_charge_cable",),
                required=("charge", "cable"),
                domain="binary_sensor",
            ),
            ("binary_sensor.tesla_model_y_charge_cable",),
            "Charging connection state",
        ),
        (
            OPT_EV_SOC_ENTITY,
            _first_state(
                states,
                exact_ids=("sensor.tesla_model_y_battery_level",),
                required=("battery", "level"),
                domain="sensor",
            ),
            ("sensor.tesla_model_y_battery_level",),
            "Vehicle state of charge",
        ),
        (
            OPT_EV_TARGET_SOC_ENTITY,
            _first_state(
                states,
                exact_ids=("number.tesla_model_y_charge_limit",),
                required=("charge", "limit"),
                domain="number",
            ),
            ("number.tesla_model_y_charge_limit",),
            "Vehicle charge target",
        ),
        (
            OPT_EV_ENERGY_REMAINING_ENTITY,
            _first_state(
                states,
                exact_ids=("sensor.tesla_model_y_energy_remaining",),
                required=("energy", "remaining"),
                domain="sensor",
            ),
            ("sensor.tesla_model_y_energy_remaining",),
            "Vehicle energy remaining",
        ),
    )
    matched_ev: dict[str, State | None] = {}
    for key, state, exact_ids, detail in ev_matches:
        matched_ev[key] = state
        record_state(key, state, exact_ids=exact_ids, detail=detail)
    raw_current_min = _attribute_number(ev_current, "min")
    raw_current_max = _attribute_number(ev_current, "max")
    raw_current_step = _attribute_number(ev_current, "step")
    proposed_min = raw_current_min
    if ev_current is not None:
        # Tessie exposes 0 A as the selector minimum, but this installation's
        # commissioned AC charging floor is 5 A. This is a proposal only: the
        # EV review screen requires the user to confirm it together with phase
        # count and voltage before planning is enabled.
        proposed_min = (
            5.0
            if ev_current.entity_id == "number.tesla_model_y_charge_current"
            and raw_current_min == 0
            else raw_current_min
        )
    ev_soc = matched_ev[OPT_EV_SOC_ENTITY]
    ev_remaining = matched_ev[OPT_EV_ENERGY_REMAINING_ENTITY]
    soc = _number(ev_soc)
    remaining = _as_kwh(ev_remaining)
    if soc is not None and remaining is not None and 1 <= soc <= 100:
        record(
            OPT_EV_BATTERY_KWH,
            round(remaining / (soc / 100), 2),
            source="derived_vehicle_state",
            confidence="medium",
            detail="Derived as remaining energy divided by current SOC",
        )

    # Existing confirmations survive rediscovery; inference alone never turns
    # a meter into a controllable or deferrable load.
    confirmation_pairs = (
        (OPT_POOL_PLANNING_ENABLED, OPT_POOL_DEFERRABLE_CONFIRMED),
        (OPT_BOILER_PLANNING_ENABLED, OPT_BOILER_DEFERRABLE_CONFIRMED),
        (OPT_EV_PLANNING_ENABLED, OPT_EV_DEFERRABLE_CONFIRMED),
    )
    for enabled_key, confirmation_key in confirmation_pairs:
        options[enabled_key] = bool(
            existing.get(enabled_key) and existing.get(confirmation_key)
        )
        options[confirmation_key] = bool(existing.get(confirmation_key))
    options[OPT_EV_ELECTRICAL_CONFIRMED] = bool(
        existing.get(OPT_EV_ELECTRICAL_CONFIRMED)
    )

    def missing(keys: tuple[str, ...]) -> list[str]:
        return [
            key
            for key in keys
            if options.get(key) in (None, "", [])
        ]

    pool_candidate = bool(mapped.get("pool_heating"))
    boiler_candidate = bool(mapped.get("hot_water"))
    ev_candidate = bool(
        mapped.get("ev_charging")
        or matched_ev.get(OPT_EV_CONNECTED_ENTITY)
        or ev_current
    )
    capabilities = {
        "metering": {
            "detected": sum(len(value) for value in mapped.values()),
            "source": "energy_dashboard",
        },
        "pv": {
            "candidate": bool(pv_forecasts),
            "ready": bool(pv_forecasts),
            "missing": [],
        },
        "battery": {
            "candidate": battery_soc is not None,
            "ready": battery_soc is not None and not missing((
                OPT_BATTERY_CAPACITY_KWH,
                OPT_BATTERY_CHARGE_MAX_W,
                OPT_BATTERY_DISCHARGE_MAX_W,
            )),
            "missing": missing((
                OPT_BATTERY_CAPACITY_KWH,
                OPT_BATTERY_CHARGE_MAX_W,
                OPT_BATTERY_DISCHARGE_MAX_W,
            )),
        },
        "pool": {
            "candidate": pool_candidate,
            "ready_after_review": pool_candidate,
            "missing": [],
            "requires_confirmation": True,
        },
        "boiler": {
            "candidate": boiler_candidate,
            "ready_after_review": boiler_candidate,
            "missing": [],
            "requires_confirmation": True,
        },
        "ev": {
            "candidate": ev_candidate,
            "ready_after_review": ev_candidate and not missing((
                OPT_EV_CONNECTED_ENTITY,
                OPT_EV_SOC_ENTITY,
                OPT_EV_TARGET_SOC_ENTITY,
                OPT_EV_BATTERY_KWH,
            )) and ev_current is not None,
            "missing": missing((
                OPT_EV_CONNECTED_ENTITY,
                OPT_EV_SOC_ENTITY,
                OPT_EV_TARGET_SOC_ENTITY,
                OPT_EV_BATTERY_KWH,
            )),
            "current_control": (
                None
                if ev_current is None
                else {
                    "entity_id": ev_current.entity_id,
                    "raw_min_a": raw_current_min,
                    "raw_max_a": raw_current_max,
                    "raw_step_a": raw_current_step,
                    "proposed_min_a": proposed_min,
                    "proposed_max_a": raw_current_max,
                    "proposed_step_a": raw_current_step,
                }
            ),
            "requires_confirmation": True,
            "electrical_values_require_confirmation": True,
        },
    }
    for key in (
        OPT_BATTERY_CAPACITY_KWH,
        OPT_BATTERY_CHARGE_MAX_W,
        OPT_BATTERY_DISCHARGE_MAX_W,
        OPT_GRID_IMPORT_LIMIT_W,
        OPT_GRID_EXPORT_LIMIT_W,
        OPT_EV_CONNECTED_ENTITY,
        OPT_EV_SOC_ENTITY,
        OPT_EV_TARGET_SOC_ENTITY,
        OPT_EV_BATTERY_KWH,
    ):
        if key in evidence or options.get(key) in (None, "", []):
            continue
        evidence[key] = {
            "source": "existing_configuration",
            "confidence": "requires_review",
            "detail": "Previously saved value; current discovery did not verify it",
        }
    options[OPT_AUTOMATIC_SETUP] = True
    options[OPT_DISCOVERY_EVIDENCE] = evidence
    return {
        "configuration": options,
        "capabilities": capabilities,
        "evidence": evidence,
        "review_required": [
            name
            for name in ("pool", "boiler", "ev")
            if capabilities[name]["candidate"]
        ],
    }
