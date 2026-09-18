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
        OPT_BATTERY_CONTROL_ENABLED,
        OPT_BATTERY_ENABLED,
        OPT_BATTERY_MODE_CHARGE,
        OPT_BATTERY_MODE_DISCHARGE,
        OPT_BATTERY_MODE_ENTITY,
        OPT_BATTERY_MODE_IDLE,
        OPT_BATTERY_CHARGE_LIMIT_ENTITY,
        OPT_BATTERY_DISCHARGE_LIMIT_ENTITY,
        OPT_BATTERY_CHARGING_ENTITY,
        OPT_BATTERY_DISCHARGING_ENTITY,
        OPT_BATTERY_MODE_BASELINE,
        OPT_BATTERY_POWER_MEASUREMENT_ENTITY,
        OPT_BATTERY_SOC_ENTITY,
    )
except ImportError:  # pragma: no cover - flat import path
    from const import (  # type: ignore[no-redef]
        OPT_BATTERY_CONTROL_ENABLED,
        OPT_BATTERY_ENABLED,
        OPT_BATTERY_MODE_CHARGE,
        OPT_BATTERY_MODE_DISCHARGE,
        OPT_BATTERY_MODE_ENTITY,
        OPT_BATTERY_MODE_IDLE,
        OPT_BATTERY_CHARGE_LIMIT_ENTITY,
        OPT_BATTERY_DISCHARGE_LIMIT_ENTITY,
        OPT_BATTERY_CHARGING_ENTITY,
        OPT_BATTERY_DISCHARGING_ENTITY,
        OPT_BATTERY_MODE_BASELINE,
        OPT_BATTERY_POWER_MEASUREMENT_ENTITY,
        OPT_BATTERY_SOC_ENTITY,
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


def room_thermal_zones(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the planned device views that feed a room heat model.

    The views also hold the $battery, $ev and $pool system entries, which carry
    no website control type and may lack a planning role.
    """
    return [
        device
        for device in devices
        if device.get("planning_role") == "controllable"
        and is_room_thermal_control(device.get("control_type"), device.get("category"))
        and (
            device["control_type"] == "setpoint"
            or bool(device.get("mapping", {}).get("temperature_entity_id"))
        )
    ]


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


def _field_error(errors, field_errors, message, *keys):
    errors.append(message)
    if field_errors is not None:
        for key in keys:
            field_errors.setdefault(key, []).append(message)


def _offset_errors(mapping: dict[str, Any], field_errors=None) -> list[str]:
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
        _field_error(errors, field_errors, "minimum offset is required with an offset entity", "offset_minimum")
    if maximum is None:
        _field_error(errors, field_errors, "maximum offset is required with an offset entity", "offset_maximum")
    if minimum is not None and maximum is not None and minimum >= maximum:
        _field_error(errors, field_errors, "minimum offset must be below maximum offset", "offset_minimum", "offset_maximum")
    return errors



def mapping_errors(
    mapping: dict[str, Any],
    control_type: str,
    *,
    room_control: bool = False,
    field_errors: dict[str, list[str]] | None = None,
) -> list[str]:
    """Return structural mapping errors for one control contract."""
    if control_type not in CONTROL_TYPES:
        return ["unsupported control type"]
    if mapping.get("control_type") != control_type:
        return ["the saved mapping belongs to a different control type"]

    errors: list[str] = []
    if mapping.get("companion_actuator_entity_ids"):
        _field_error(errors, field_errors, "configure combined switching in Home Assistant using one control entity", "companion_actuator_entity_ids")
    actuators = mapping.get("actuator_entity_ids")
    if isinstance(actuators, list) and len(actuators) > 1:
        _field_error(errors, field_errors, "choose exactly one control entity", "actuator_entity_ids")
    if (control_type == "setpoint" or room_control) and not _text(
        mapping, "temperature_entity_id"
    ):
        _field_error(errors, field_errors, "room temperature entity is required", "temperature_entity_id")
    if control_type == "setpoint":
        if not _entities(mapping, "actuator_entity_ids"):
            _field_error(errors, field_errors, "one heater or climate actuator is required", "actuator_entity_ids")
        errors.extend(_offset_errors(mapping, field_errors))
    elif control_type == "permit_inhibit":
        if not _entities(mapping, "actuator_entity_ids"):
            _field_error(errors, field_errors, "one permit/inhibit actuator is required", "actuator_entity_ids")
        if not _positive_number(mapping, "max_inhibit_slots"):
            _field_error(errors, field_errors, "maximum inhibit slots must be positive", "max_inhibit_slots")
    elif control_type == "switch_schedule":
        if not _entities(mapping, "actuator_entity_ids"):
            _field_error(errors, field_errors, "one switch actuator is required", "actuator_entity_ids")
    elif control_type == "variable_power":
        if not _text(mapping, "control_entity_id"):
            _field_error(errors, field_errors, "number control entity is required", "control_entity_id")
        minimum_valid = _non_negative_number(mapping, "minimum_value")
        if not minimum_valid:
            _field_error(errors, field_errors, "minimum control value must be zero or greater", "minimum_value")
        if not _positive_number(mapping, "maximum_value"):
            _field_error(errors, field_errors, "maximum control value must be positive", "maximum_value")
        if (
            minimum_valid
            and _positive_number(mapping, "maximum_value")
            and float(mapping["minimum_value"]) >= float(mapping["maximum_value"])
        ):
            _field_error(errors, field_errors, "minimum control value must be below maximum control value", "minimum_value", "maximum_value")
    if not _power_source(mapping):
        _field_error(errors, field_errors, "power must be a power entity or a positive watt value", "power")
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
    pool_control: bool = False,
    pool_water_entity: str | None = None,
) -> dict[str, Any]:
    """Build the privacy-minimised status uploaded to the website."""
    if pool_control and mapping and mapping.get("control_type") != requested_control_type:
        return mapping_report(requested_control_type, mapping, known_entity_ids, entity_names, area_names, entity_area_ids)
    if pool_control:
        local = {**(mapping or {}), "control_type": "switch_schedule"}
        report = mapping_report("switch_schedule", local, known_entity_ids, entity_names, area_names, entity_area_ids)
        fields = report["field_errors"]
        errors = pool_control_errors({"pool_water_temperature_entity": pool_water_entity}, local, field_errors=fields)
        if errors:
            report["mapping_status"] = "invalid"
            report["mapping_error"] = "; ".join(filter(None, [report["mapping_error"], *errors]))
        report["mapped_control_type"] = requested_control_type
        report["mapping_summary"]["control_type"] = requested_control_type
        report["mapping_summary"]["planning_service"] = "pool"
        return report
    if requested_control_type not in CONTROL_TYPES or not mapping or mapping.get("control_type") != requested_control_type:
        field_errors = {}
        if requested_control_type in CONTROL_TYPES:
            mapping_errors({"control_type": requested_control_type}, requested_control_type,
                           room_control=room_control, field_errors=field_errors)
        return {
            "field_errors": field_errors,
            "mapping_status": "not_configured",
            "mapped_control_type": None,
            "mapping_error": None,
            "mapping_summary": {},
        }
    mapped_control_type = mapping.get("control_type")
    room_control = requested_control_type == "setpoint" or (
        room_control and _text(mapping, "temperature_entity_id")
    )
    field_errors = {}
    errors = mapping_errors(
        mapping,
        requested_control_type,
        room_control=room_control, field_errors=field_errors,
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
        for key in (*active_entity_fields, "power"):
            value = mapping.get(key)
            for entity_id in value if isinstance(value, list) else [value]:
                if isinstance(entity_id, str) and "." in entity_id and entity_id not in known_entity_ids:
                    _field_error([], field_errors, f"{entity_id} no longer exists", key)
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
        "field_errors": field_errors,
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
    *, pool_water_entity: str | None = None,
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
                pool_control=mapped_planning_path({"category": category, "control_type": requested_control}, mappings.get(device["key"], {}), pool_water_entity) == "pool",
                pool_water_entity=pool_water_entity,
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
        device.update({key: value for key, value in report.items() if key != "field_errors"})
        reviewed_power = report["mapping_summary"].get("reviewed_power_w")
        if ready and isinstance(reviewed_power, (int, float)):
            device["active_power_w"] = float(reviewed_power)
    return devices


BATTERY_MEASUREMENT_FIELDS = {
    "battery_power_measurement_entity": "Measured battery power",
    "house_consumption_power_entity": "Instantaneous house consumption",
    "solar_production_power_entity": "Instantaneous solar production",
    "grid_power_entity": "Signed grid power",
}


def battery_measurement_errors(options, *, field_errors=None):
    """Report every missing or duplicated physical measurement at its editor."""
    errors = []
    for key, label in BATTERY_MEASUREMENT_FIELDS.items():
        if not _text(options, key):
            _field_error(errors, field_errors, f"{label} is required for battery control", key)
    for entity in dict.fromkeys(options[key] for key in BATTERY_MEASUREMENT_FIELDS if _text(options, key)):
        keys = [key for key in BATTERY_MEASUREMENT_FIELDS if options.get(key) == entity]
        if len(keys) > 1:
            labels = ", ".join(BATTERY_MEASUREMENT_FIELDS[key] for key in keys)
            _field_error(errors, field_errors, f"{labels} must use distinct measurement sources", *keys)
    return errors


class BatteryMeasurementConfigurationError(ValueError):
    """A configuration failure with destinations independent of message wording."""
    def __init__(self, options):
        fields = {}
        errors = battery_measurement_errors(options, field_errors=fields)
        super().__init__("; ".join(errors))
        self.fix = {"kind": "fields", "fields": [
            {"key": key, "message": "; ".join(messages)} for key, messages in fields.items()]}
        self.next_step = "Select a separate power sensor for each highlighted measurement. Use the field links to open its settings."


def battery_control_errors(options: dict[str, Any], *, field_errors: dict[str, list[str]] | None = None) -> list[str]:
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
    errors = battery_measurement_errors(options, field_errors=field_errors)
    if not options.get(OPT_BATTERY_ENABLED):
        _field_error(errors, field_errors, "this home is not marked as having a house battery", OPT_BATTERY_ENABLED)
    for key, label in (
        (OPT_BATTERY_MODE_ENTITY, "battery mode entity"),
        (OPT_BATTERY_CHARGE_LIMIT_ENTITY, "charge power limit entity"),
        (OPT_BATTERY_DISCHARGE_LIMIT_ENTITY, "discharge power limit entity"),
        (OPT_BATTERY_CHARGING_ENTITY, "battery charging sensor"),
        (OPT_BATTERY_DISCHARGING_ENTITY, "battery discharging sensor"),
        (OPT_BATTERY_SOC_ENTITY, "battery state of charge entity"),
    ):
        if not _text(options, key):
            _field_error(errors, field_errors, f"{label} is required", key)
    # Naming the modes is what makes a flow reversal expressible at all. A
    # mapping without them can raise and lower a number that the inverter is
    # not in a mode to honour.
    for key, label in (
        (OPT_BATTERY_MODE_CHARGE, "charge"),
        (OPT_BATTERY_MODE_DISCHARGE, "discharge"),
        (OPT_BATTERY_MODE_IDLE, "idle"),
    ):
        if not _text(options, key):
            _field_error(errors, field_errors, f"the mode value meaning {label} is required", key)
        elif str(options[key]).startswith("binary_sensor."):
            _field_error(errors, field_errors, f"{label} mode must be a selector option, not a direction sensor", key)
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
        _field_error(errors, field_errors, "charge, discharge and idle must be different mode values", OPT_BATTERY_MODE_CHARGE, OPT_BATTERY_MODE_DISCHARGE, OPT_BATTERY_MODE_IDLE)
    if not _text(options, OPT_BATTERY_MODE_BASELINE):
        _field_error(errors, field_errors, "baseline mode is required", OPT_BATTERY_MODE_BASELINE)
    if options.get(OPT_BATTERY_CHARGE_LIMIT_ENTITY) and options.get(OPT_BATTERY_CHARGE_LIMIT_ENTITY) == options.get(OPT_BATTERY_DISCHARGE_LIMIT_ENTITY):
        _field_error(errors, field_errors, "charge and discharge limits must be different entities", OPT_BATTERY_CHARGE_LIMIT_ENTITY, OPT_BATTERY_DISCHARGE_LIMIT_ENTITY)
    if options.get(OPT_BATTERY_CHARGING_ENTITY) and options.get(OPT_BATTERY_CHARGING_ENTITY) == options.get(OPT_BATTERY_DISCHARGING_ENTITY):
        _field_error(errors, field_errors, "charging and discharging sensors must be different entities", OPT_BATTERY_CHARGING_ENTITY, OPT_BATTERY_DISCHARGING_ENTITY)
    return errors


def pool_control_errors(options, mapping=None, *, field_errors=None):
    """The pool only controls a mapped on/off switch and reads water temperature."""
    errors = []
    if not options.get("pool_water_temperature_entity"):
        _field_error(errors, field_errors, "Select the pool water temperature sensor", "pool_water_temperature_entity")
    if mapping is not None:
        actuators = mapping.get("actuator_entity_ids") or []
        if len(actuators) != 1 or not isinstance(actuators[0], str) or actuators[0].split(".")[0] not in ("switch", "input_boolean"):
            _field_error(errors, field_errors, "Select one on/off Control entity for the pool heater", "actuator_entity_ids")
    return errors


def pool_control_mapping(options, devices):
    """Resolve the same physical owner as the pool card, without a second binding."""
    if __package__:
        from .operating_modes import system_device_keys
    else:
        from operating_modes import system_device_keys
    owners = system_device_keys(devices, options)
    key = next((key for key, system in owners.items() if system == "pool"), None)
    return key, options.get("device_control_mappings", {}).get(key, {})


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
