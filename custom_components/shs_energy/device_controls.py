"""Local entity mappings for website-requested controllable devices.

The website owns the requested planning role. Home Assistant owns entity ids
and installation-specific limits. A device is effective as controllable only
when both sides name the same control type and this local mapping is complete.
"""

from __future__ import annotations

from math import isfinite
from typing import Any

try:  # pragma: no cover - package in HA, flat module in the pure test suite
    from .const import (
        BATTERY_POWER_UNITS,
        OPT_BATTERY_AUTHORITY_CONFIRM_ENTITY,
        OPT_BATTERY_AUTHORITY_CONFIRM_STATE,
        OPT_BATTERY_AUTHORITY_ENTITY,
        OPT_BATTERY_CONTROL_ENABLED,
        OPT_BATTERY_ENABLED,
        OPT_BATTERY_MODE_CHARGE,
        OPT_BATTERY_MODE_DISCHARGE,
        OPT_BATTERY_MODE_ENTITY,
        OPT_BATTERY_MODE_IDLE,
        OPT_BATTERY_POWER_ENTITY,
        OPT_BATTERY_POWER_MEASUREMENT_ENTITY,
        OPT_BATTERY_POWER_UNIT,
        OPT_BATTERY_SOC_ENTITY,
        OPT_POOL_ENABLED,
        OPT_POOL_START_TEMPERATURE_ENTITY,
        OPT_POOL_STOP_TEMPERATURE_ENTITY,
        OPT_POOL_TEMPERATURE_MAXIMUM,
        OPT_POOL_TEMPERATURE_MINIMUM,
    )
except ImportError:  # pragma: no cover - flat import path
    from const import (  # type: ignore[no-redef]
        BATTERY_POWER_UNITS,
        OPT_BATTERY_AUTHORITY_CONFIRM_ENTITY,
        OPT_BATTERY_AUTHORITY_CONFIRM_STATE,
        OPT_BATTERY_AUTHORITY_ENTITY,
        OPT_BATTERY_CONTROL_ENABLED,
        OPT_BATTERY_ENABLED,
        OPT_BATTERY_MODE_CHARGE,
        OPT_BATTERY_MODE_DISCHARGE,
        OPT_BATTERY_MODE_ENTITY,
        OPT_BATTERY_MODE_IDLE,
        OPT_BATTERY_POWER_ENTITY,
        OPT_BATTERY_POWER_MEASUREMENT_ENTITY,
        OPT_BATTERY_POWER_UNIT,
        OPT_BATTERY_SOC_ENTITY,
        OPT_POOL_ENABLED,
        OPT_POOL_START_TEMPERATURE_ENTITY,
        OPT_POOL_STOP_TEMPERATURE_ENTITY,
        OPT_POOL_TEMPERATURE_MAXIMUM,
        OPT_POOL_TEMPERATURE_MINIMUM,
    )

CONTROL_TYPES = (
    "switch_schedule",
    "variable_power",
    "permit_inhibit",
    "setpoint",
)

if __package__:
    from .const import ROOM_AREA_FIELD
else:
    from const import ROOM_AREA_FIELD

_ENTITY_FIELDS_BY_CONTROL_TYPE: dict[str, tuple[str, ...]] = {
    "setpoint": (
        "temperature_entity_id",
        "setpoint_entity_id",
        "actuator_entity_ids",
        "companion_actuator_entity_ids",
        # The thermal executor may request a bounded offset, a demand mode, or
        # a permission instead of a temperature. A machine with its own
        # controller — a heat pump reached over Modbus — accepts these and does
        # not accept arbitrary watts, so a mapping that can only carry a
        # setpoint cannot express how it is actually driven. All optional: an
        # existing setpoint mapping stays complete without them.
        "offset_entity_id",
        "mode_entity_id",
        "permit_entity_id",
    ),
    "permit_inhibit": ("actuator_entity_ids",),
    "switch_schedule": (
        "temperature_entity_id",
        "actuator_entity_ids",
        "companion_actuator_entity_ids",
    ),
    "variable_power": ("control_entity_id",),
}

def _present(value: Any) -> bool:
    return value not in (None, "", [])


def is_room_thermal_control(control_type: str | None, category: str | None) -> bool:
    """Return whether one requested device belongs to a room heat model."""
    return control_type == "setpoint" or (
        control_type == "switch_schedule" and category in ("heating", "cooling")
    )


def planning_path(control_type: str | None, category: str | None) -> str | None:
    """Return which planning model owns one controllable device.

    This is the single authority on that routing. The website offers every
    control type for every meter category, so a category never implies a
    control contract: a pool-room floor heater metered as ``pool_heating`` but
    asked to hold a setpoint belongs to its room's heat model, not to the pool
    service. Callers that assumed the reverse turned one such pairing into a
    whole-plan failure.

    ``None`` means no model can plan the pairing, which keeps the meter in
    base load instead of leaving it silently unscheduled.
    """
    if is_room_thermal_control(control_type, category):
        return "room"
    if control_type == "switch_schedule" and category == "pool_heating":
        return "pool"
    if control_type == "permit_inhibit" and category == "hot_water":
        return "boiler"
    if control_type == "variable_power" and category == "ev_charging":
        return "ev"
    return None


def mapped_planning_path(
    device: dict[str, Any], mapping: dict[str, Any], pool_water_entity: str | None,
) -> str | None:
    """Resolve pool water heating by its sensor, not its room or meter name."""
    if (
        device.get("category") == "pool_heating"
        and device.get("control_type") == "setpoint"
        and pool_water_entity
        and mapping.get("temperature_entity_id") == pool_water_entity
    ):
        return "pool"
    return planning_path(device.get("control_type"), device.get("category"))


def apply_planner_support(
    report: dict[str, Any],
    control_type: str | None,
    category: str | None,
) -> dict[str, Any]:
    """Fail a locally complete mapping that no planning model can use."""
    if report["mapping_status"] != "ready" or planning_path(
        control_type, category
    ) is not None:
        return report
    return {
        **report,
        "mapping_status": "invalid",
        "mapping_error": (
            f"the planner has no model for {str(control_type).replace('_', ' ')} "
            f"control of a {str(category or 'unknown').replace('_', ' ')} meter; "
            "choose another control type on the website or leave this meter in "
            "base load"
        ),
    }


def _text(mapping: dict[str, Any], key: str) -> bool:
    return isinstance(mapping.get(key), str) and bool(mapping[key].strip())


def _entities(mapping: dict[str, Any], key: str) -> bool:
    values = mapping.get(key)
    return (
        isinstance(values, list)
        and bool(values)
        and all(isinstance(value, str) and bool(value.strip()) for value in values)
    )


def _positive_number(mapping: dict[str, Any], key: str) -> bool:
    try:
        value = float(mapping.get(key))
    except (TypeError, ValueError):
        return False
    return isfinite(value) and value > 0


def _non_negative_number(mapping: dict[str, Any], key: str) -> bool:
    try:
        value = float(mapping.get(key))
    except (TypeError, ValueError):
        return False
    return isfinite(value) and value >= 0


def _power_source(mapping: dict[str, Any]) -> bool:
    value = mapping.get("power")
    return value in (None, "") or _positive_number(mapping, "power") or (
        isinstance(value, str) and "." in value and bool(value.strip())
    )


def _number(mapping: dict[str, Any], key: str) -> float | None:
    try:
        value = float(mapping.get(key))
    except (TypeError, ValueError):
        return None
    return value if isfinite(value) else None


def _offset_errors(mapping: dict[str, Any]) -> list[str]:
    """A bounded offset is only usable when its bounds are actually stated.

    An unbounded offset entity is the one lever here that can drive equipment
    past what was reviewed, so the bounds are required with it rather than
    optional beside it. Without the entity there is nothing to bound.
    """
    if not _text(mapping, "offset_entity_id"):
        return []
    minimum = _number(mapping, "offset_minimum")
    maximum = _number(mapping, "offset_maximum")
    errors: list[str] = []
    if minimum is None:
        errors.append("minimum offset is required with an offset entity")
    if maximum is None:
        errors.append("maximum offset is required with an offset entity")
    if minimum is not None and maximum is not None and minimum >= maximum:
        errors.append("minimum offset must be below maximum offset")
    return errors


def _band_errors(
    values: dict[str, Any],
    *,
    start_key: str,
    stop_key: str,
    minimum_key: str,
    maximum_key: str,
    subject: str,
) -> list[str]:
    """A hysteresis band is a pair, and it stays inside a reviewed range.

    Both ends are required together because writing one alone inverts or
    collapses the window the equipment runs to. The range is required with them
    for the same reason the thermal offset's is: without it the planner could
    ask for any temperature the register accepts.
    """
    start = _text(values, start_key)
    stop = _text(values, stop_key)
    if not start and not stop:
        return []
    errors: list[str] = []
    if not start:
        errors.append(f"a {subject} start temperature entity is required with a stop temperature")
    if not stop:
        errors.append(f"a {subject} stop temperature entity is required with a start temperature")
    minimum = _number(values, minimum_key)
    maximum = _number(values, maximum_key)
    if minimum is None:
        errors.append(f"a minimum {subject} temperature is required with a temperature band")
    if maximum is None:
        errors.append(f"a maximum {subject} temperature is required with a temperature band")
    if minimum is not None and maximum is not None and minimum >= maximum:
        errors.append(f"the minimum {subject} temperature must be below the maximum")
    return errors


def mapping_errors(
    mapping: dict[str, Any],
    control_type: str,
    *,
    room_control: bool = False,
) -> list[str]:
    """Return structural mapping errors for one control contract."""
    if control_type not in CONTROL_TYPES:
        return ["unsupported control type"]
    if mapping.get("control_type") != control_type:
        return ["the saved mapping belongs to a different control type"]

    errors: list[str] = []
    if (control_type == "setpoint" or room_control) and not _text(
        mapping, "temperature_entity_id"
    ):
        errors.append("room temperature entity is required")
    if control_type == "setpoint":
        if not _entities(mapping, "actuator_entity_ids"):
            errors.append("at least one heater or climate actuator is required")
        errors.extend(_offset_errors(mapping))
    elif control_type == "permit_inhibit":
        if not _entities(mapping, "actuator_entity_ids"):
            errors.append("at least one permit/inhibit actuator is required")
        if not _positive_number(mapping, "max_inhibit_slots"):
            errors.append("maximum inhibit slots must be positive")
    elif control_type == "switch_schedule":
        if not _entities(mapping, "actuator_entity_ids"):
            errors.append("at least one switch actuator is required")
    elif control_type == "variable_power":
        if not _text(mapping, "control_entity_id"):
            errors.append("number control entity is required")
        minimum_valid = _non_negative_number(mapping, "minimum_value")
        if not minimum_valid:
            errors.append("minimum control value must be zero or greater")
        if not _positive_number(mapping, "maximum_value"):
            errors.append("maximum control value must be positive")
        if (
            minimum_valid
            and _positive_number(mapping, "maximum_value")
            and float(mapping["minimum_value"]) >= float(mapping["maximum_value"])
        ):
            errors.append("minimum control value must be below maximum control value")
    if not _power_source(mapping):
        errors.append("power must be a power entity or a positive watt value")
    return errors


def mapping_report(
    requested_control_type: str | None,
    mapping: dict[str, Any] | None,
    known_entity_ids: set[str] | None = None,
    entity_names: dict[str, str] | None = None,
    area_names: dict[str, str] | None = None,
    entity_area_ids: dict[str, str] | None = None,
    *,
    room_control: bool = False,
) -> dict[str, Any]:
    """Build the privacy-minimised status uploaded to the website."""
    if requested_control_type not in CONTROL_TYPES or not mapping:
        return {
            "mapping_status": "not_configured",
            "mapped_control_type": None,
            "mapping_error": None,
            "mapping_summary": {},
        }
    mapped_control_type = mapping.get("control_type")
    if mapped_control_type != requested_control_type:
        return {
            "mapping_status": "not_configured",
            "mapped_control_type": None,
            "mapping_error": None,
            "mapping_summary": {},
        }
    room_control = requested_control_type == "setpoint" or (
        room_control and _text(mapping, "temperature_entity_id")
    )
    errors = mapping_errors(
        mapping,
        requested_control_type,
        room_control=room_control,
    )
    active_entity_fields = _ENTITY_FIELDS_BY_CONTROL_TYPE.get(
        requested_control_type, ()
    )
    configured_entity_ids = {
        entity_id
        for key in active_entity_fields
        for value in [mapping.get(key)]
        for entity_id in (value if isinstance(value, list) else [value])
        if isinstance(entity_id, str) and entity_id
    }
    if isinstance(mapping.get("power"), str) and "." in mapping["power"]:
        configured_entity_ids.add(mapping["power"])
    if known_entity_ids is not None and not configured_entity_ids.issubset(
        known_entity_ids
    ):
        errors.append("one or more configured entities no longer exist")
    area_id: str | None = None
    if room_control:
        actuators = [
            value
            for value in mapping.get("actuator_entity_ids", [])
            if isinstance(value, str) and value
        ]
        existing_actuators = [
            value
            for value in actuators
            if known_entity_ids is None or value in known_entity_ids
        ]
        missing_areas = [
            value
            for value in existing_actuators
            if not (entity_area_ids or {}).get(value)
        ]
        saved_room_area = mapping.get(ROOM_AREA_FIELD)
        saved_room_valid = (
            isinstance(saved_room_area, str)
            and bool(saved_room_area.strip())
            and (area_names is None or saved_room_area in area_names)
        )
        if missing_areas and not saved_room_valid:
            labels = ", ".join(
                (entity_names or {}).get(value, value) for value in missing_areas
            )
            errors.append(
                "assign a Home Assistant area to every controlled actuator "
                f"before saving: {labels}"
            )
        actuator_areas = {
            (entity_area_ids or {}).get(value)
            for value in existing_actuators
            if (entity_area_ids or {}).get(value)
        }
        if len(actuator_areas) > 1:
            labels = ", ".join(
                sorted((area_names or {}).get(value, value) for value in actuator_areas)
            )
            errors.append(
                "controlled actuators must all belong to one Home Assistant "
                f"room; found: {labels}"
            )
        elif not missing_areas and len(actuator_areas) == 1:
            area_id = next(iter(actuator_areas))
            if area_names is not None and area_id not in area_names:
                errors.append("the controlled actuator's Home Assistant room no longer exists")
        elif saved_room_valid and (
            not actuator_areas or actuator_areas == {saved_room_area}
        ):
            area_id = saved_room_area
        elif isinstance(saved_room_area, str) and area_names is not None:
            if saved_room_area not in area_names:
                errors.append("the saved Home Assistant room no longer exists")
            elif actuator_areas:
                errors.append(
                    "the controlled actuator room conflicts with the saved room"
                )
    entity_count = sum(
        len(value) if isinstance(value, list) else 1
        for key in active_entity_fields
        for value in [mapping.get(key)]
        if value
    )
    summary = {
        "control_type": requested_control_type,
        "entity_count": entity_count,
        "configured_fields": sorted(
            key
            for key in (
                *active_entity_fields,
                "power",
                "max_inhibit_slots",
                "minimum_value",
                "maximum_value",
                "offset_minimum",
                "offset_maximum",
            )
            if _present(mapping.get(key))
        ),
    }
    if room_control and isinstance(area_id, str):
        summary.update(
            {
                "room_key": area_id,
                "room_name": (area_names or {}).get(area_id, area_id),
                "controlled_devices": sorted(
                    {
                        (entity_names or {}).get(entity_id, entity_id)
                        for key in (
                            "actuator_entity_ids",
                            "companion_actuator_entity_ids",
                        )
                        for entity_id in mapping.get(key, [])
                        if isinstance(entity_id, str) and entity_id
                    }
                ),
            }
        )
    if _positive_number(mapping, "power"):
        summary["reviewed_power_w"] = float(mapping["power"])
    elif isinstance(mapping.get("power"), str) and mapping["power"].strip():
        summary["power_entity_name"] = (entity_names or {}).get(
            mapping["power"], mapping["power"]
        )
    return {
        "mapping_status": "invalid" if errors else "ready",
        "mapped_control_type": requested_control_type,
        "mapping_error": "; ".join(errors) if errors else None,
        "mapping_summary": summary,
    }


def apply_requested_configuration(
    devices: list[dict[str, Any]],
    requested: dict[str, dict[str, Any]],
    mappings: dict[str, dict[str, Any]],
    known_entity_ids: set[str] | None = None,
    entity_names: dict[str, str] | None = None,
    area_names: dict[str, str] | None = None,
    entity_area_ids: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Apply website requests, keeping incomplete controls in base load."""
    for device in devices:
        configuration = requested.get(device["key"], {})
        requested_role = configuration.get("planning_role", "base_load")
        requested_control = configuration.get("control_type")
        category = str(
            device.get("category") or configuration.get("category") or ""
        )
        report = apply_planner_support(
            mapping_report(
                requested_control if requested_role == "controllable" else None,
                mappings.get(device["key"]),
                known_entity_ids,
                entity_names,
                area_names,
                entity_area_ids,
                room_control=is_room_thermal_control(requested_control, category),
            ),
            requested_control if requested_role == "controllable" else None,
            category,
        )
        device["load_type"] = configuration.get(
            "load_type", device["suggested_load_type"]
        )
        ready = requested_role == "controllable" and report["mapping_status"] == "ready"
        device["planning_role"] = "controllable" if ready else "base_load"
        device["control_type"] = requested_control if ready else None
        device.update(report)
        reviewed_power = report["mapping_summary"].get("reviewed_power_w")
        if ready and isinstance(reviewed_power, (int, float)):
            device["active_power_w"] = float(reviewed_power)
    return devices


def battery_control_errors(options: dict[str, Any]) -> list[str]:
    """Return what still stops the storage executor from commanding a battery.

    Plant-level rather than a device control type. There is one battery, the
    planner already models it as a store with its own charge and discharge
    variables, and giving it a control type would route it through
    ``planning_path`` as a controllable load as well — subtracting it from base
    load and scheduling it a second time.

    Every gap is reported in one pass, for the same reason device mappings are:
    commissioning a battery one rediscovered missing field at a time is how a
    half-configured executor gets switched on.
    """
    if not options.get(OPT_BATTERY_CONTROL_ENABLED):
        return []
    errors: list[str] = []
    if not options.get(OPT_BATTERY_ENABLED):
        errors.append("this home is not marked as having a house battery")
    for key, label in (
        (OPT_BATTERY_MODE_ENTITY, "battery mode entity"),
        (OPT_BATTERY_POWER_ENTITY, "battery power target entity"),
        (OPT_BATTERY_POWER_MEASUREMENT_ENTITY, "measured battery power entity"),
        (OPT_BATTERY_SOC_ENTITY, "battery state of charge entity"),
    ):
        if not _text(options, key):
            errors.append(f"{label} is required")
    # Naming the modes is what makes a flow reversal expressible at all. A
    # mapping without them can raise and lower a number that the inverter is
    # not in a mode to honour.
    for key, label in (
        (OPT_BATTERY_MODE_CHARGE, "charge"),
        (OPT_BATTERY_MODE_DISCHARGE, "discharge"),
        (OPT_BATTERY_MODE_IDLE, "idle"),
    ):
        if not _text(options, key):
            errors.append(f"the mode value meaning {label} is required")
    modes = [
        options.get(key)
        for key in (
            OPT_BATTERY_MODE_CHARGE,
            OPT_BATTERY_MODE_DISCHARGE,
            OPT_BATTERY_MODE_IDLE,
        )
        if _text(options, key)
    ]
    if len(modes) != len(set(modes)):
        errors.append("charge, discharge and idle must be different mode values")
    unit = options.get(OPT_BATTERY_POWER_UNIT)
    if unit not in BATTERY_POWER_UNITS:
        errors.append(
            "battery power unit must be one of: " + ", ".join(BATTERY_POWER_UNITS)
        )
    # The handshake is two halves. Claiming remote control without reading back
    # whether it was granted leaves the executor unable to tell authority it
    # never had from authority it has lost.
    claims = _text(options, OPT_BATTERY_AUTHORITY_ENTITY)
    confirms = _text(options, OPT_BATTERY_AUTHORITY_CONFIRM_ENTITY)
    if claims and not confirms:
        errors.append(
            "a confirmation entity is required alongside the authority switch"
        )
    if confirms and not _text(options, OPT_BATTERY_AUTHORITY_CONFIRM_STATE):
        errors.append("the state confirming remote control is required")
    return errors


def pool_band_errors(options: dict[str, Any]) -> list[str]:
    """Return what stops the pool's temperature band from being written.

    Plant-level, for the same reason the battery's mapping is: the pool service
    is built from every device routed to it, and a heater and its circulation
    pump share one body of water and one pair of registers. One band per store
    keeps a single writer on those registers.
    """
    if not options.get(OPT_POOL_ENABLED):
        return []
    return _band_errors(
        options,
        start_key=OPT_POOL_START_TEMPERATURE_ENTITY,
        stop_key=OPT_POOL_STOP_TEMPERATURE_ENTITY,
        minimum_key=OPT_POOL_TEMPERATURE_MINIMUM,
        maximum_key=OPT_POOL_TEMPERATURE_MAXIMUM,
        subject="pool",
    )


def requested_controllable_devices(
    configuration: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return stable website-requested controls for the options flow."""
    return sorted(
        (
            {"key": key, **value}
            for key, value in configuration.items()
            if value.get("planning_role") == "controllable"
            and value.get("control_type") in CONTROL_TYPES
        ),
        key=lambda value: (str(value.get("name") or "").lower(), value["key"]),
    )
