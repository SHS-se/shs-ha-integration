"""Local entity mappings for website-requested controllable devices.

The website owns the requested planning role. Home Assistant owns entity ids
and installation-specific limits. A device is effective as controllable only
when both sides name the same control type and this local mapping is complete.
"""

from __future__ import annotations

from math import isfinite
from typing import Any

CONTROL_TYPES = (
    "switch_schedule",
    "variable_power",
    "permit_inhibit",
    "setpoint",
)

MAPPING_SCHEMA_VERSION = 2
MAPPING_SCHEMA_VERSION_FIELD = "_mapping_schema_version"
MIGRATED_ROOM_AREA_FIELD = "_migrated_room_area_id"

_ENTITY_FIELDS_BY_CONTROL_TYPE: dict[str, tuple[str, ...]] = {
    "setpoint": (
        "temperature_entity_id",
        "setpoint_entity_id",
        "actuator_entity_ids",
        "companion_actuator_entity_ids",
    ),
    "permit_inhibit": ("actuator_entity_ids",),
    "switch_schedule": (
        "temperature_entity_id",
        "actuator_entity_ids",
        "companion_actuator_entity_ids",
    ),
    "variable_power": ("control_entity_id",),
}

_LEGACY_EV_OPTION_FIELDS = (
    ("ev_connected_entity", "connected_entity_id"),
    ("ev_soc_entity", "soc_entity_id"),
    ("ev_target_soc_entity", "target_soc_entity_id"),
    ("ev_departure_entity", "departure_entity_id"),
    ("ev_energy_remaining_entity", "energy_remaining_entity_id"),
)


def _present(value: Any) -> bool:
    return value not in (None, "", [])


def migrate_device_control_mapping(
    mapping: dict[str, Any],
    *,
    entity_area_ids: dict[str, str] | None = None,
    entity_limits: dict[str, tuple[Any, Any]] | None = None,
    recover_room: bool = True,
) -> tuple[dict[str, Any], bool]:
    """Upgrade every historical mapping contract without dropping old values.

    Canonical fields are added alongside retired fields. Keeping the original
    keys makes the migration lossless while runtime validation considers only
    the current contract.
    """
    migrated = dict(mapping)
    before = dict(migrated)
    old_version = migrated.get(MAPPING_SCHEMA_VERSION_FIELD)
    is_unversioned = not isinstance(old_version, int)

    original_control_type = migrated.get("control_type")
    if original_control_type == "current_limit":
        migrated.setdefault("_migrated_from_control_type", original_control_type)
        migrated["control_type"] = "variable_power"

    def copy_first(target: str, *sources: str) -> None:
        if _present(migrated.get(target)):
            return
        for source in sources:
            if _present(migrated.get(source)):
                migrated[target] = migrated[source]
                return

    copy_first("power", "power_entity_id", "power_w")
    if migrated.get("control_type") == "variable_power":
        copy_first(
            "control_entity_id",
            "current_control_entity_id",
            "power_control_entity_id",
        )
        copy_first("minimum_value", "min_current_a")
        copy_first("maximum_value", "max_current_a")
        control_entity_id = migrated.get("control_entity_id")
        limits = (entity_limits or {}).get(control_entity_id)
        if limits is not None:
            if not _present(migrated.get("minimum_value")) and _present(limits[0]):
                migrated["minimum_value"] = limits[0]
            if not _present(migrated.get("maximum_value")) and _present(limits[1]):
                migrated["maximum_value"] = limits[1]

    needs_room_recovery = recover_room and is_unversioned and (
        migrated.get("control_type") == "setpoint"
        or _text(migrated, "temperature_entity_id")
    )
    if needs_room_recovery:
        actuator_areas = {
            (entity_area_ids or {}).get(entity_id)
            for entity_id in migrated.get("actuator_entity_ids", [])
            if isinstance(entity_id, str)
            and (entity_area_ids or {}).get(entity_id)
        }
        room_area_id: Any = None
        if len(actuator_areas) == 1:
            room_area_id = next(iter(actuator_areas))
        elif _text(migrated, "area_id"):
            room_area_id = migrated["area_id"]
        else:
            temperature_entity_id = migrated.get("temperature_entity_id")
            if isinstance(temperature_entity_id, str):
                room_area_id = (entity_area_ids or {}).get(temperature_entity_id)
        if _present(room_area_id):
            migrated[MIGRATED_ROOM_AREA_FIELD] = room_area_id

    if not needs_room_recovery or _present(
        migrated.get(MIGRATED_ROOM_AREA_FIELD)
    ):
        migrated[MAPPING_SCHEMA_VERSION_FIELD] = MAPPING_SCHEMA_VERSION
    return migrated, migrated != before


def migrate_device_control_mappings(
    mappings: dict[str, Any],
    *,
    entity_area_ids: dict[str, str] | None = None,
    entity_limits: dict[str, tuple[Any, Any]] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Upgrade a complete mapping collection, preserving every device key."""
    migrated: dict[str, Any] = {}
    changed = False
    for device_key, raw_mapping in mappings.items():
        if not isinstance(raw_mapping, dict):
            migrated[device_key] = raw_mapping
            continue
        value, value_changed = migrate_device_control_mapping(
            raw_mapping,
            entity_area_ids=entity_area_ids,
            entity_limits=entity_limits,
        )
        migrated[device_key] = value
        changed = changed or value_changed
    return migrated, changed


def recover_legacy_ev_options(
    options: dict[str, Any],
    mappings: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Recover EV telemetry that older releases stored inside its device card."""
    migrated = dict(options)
    before = dict(migrated)
    mapping_values = [
        mapping for mapping in mappings.values() if isinstance(mapping, dict)
    ]
    for option_key, mapping_key in _LEGACY_EV_OPTION_FIELDS:
        if _present(migrated.get(option_key)):
            continue
        candidates = {
            mapping[mapping_key]
            for mapping in mapping_values
            if isinstance(mapping.get(mapping_key), str)
            and mapping[mapping_key].strip()
        }
        if len(candidates) == 1:
            migrated[option_key] = next(iter(candidates))
    return migrated, migrated != before


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
    elif control_type == "permit_inhibit":
        if not _entities(mapping, "actuator_entity_ids"):
            errors.append("at least one permit/inhibit actuator is required")
        if not _positive_number(mapping, "max_inhibit_slots"):
            errors.append("maximum inhibit slots must be positive")
    elif control_type == "switch_schedule":
        if not _entities(mapping, "actuator_entity_ids"):
            errors.append("at least one switch actuator is required")
        if "min_run_slots" in mapping and not _positive_number(
            mapping, "min_run_slots"
        ):
            errors.append("minimum run slots must be positive")
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
        migrated_room_area = mapping.get(MIGRATED_ROOM_AREA_FIELD)
        migrated_room_valid = (
            isinstance(migrated_room_area, str)
            and bool(migrated_room_area.strip())
            and (area_names is None or migrated_room_area in area_names)
        )
        if missing_areas and not migrated_room_valid:
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
        elif migrated_room_valid and (
            not actuator_areas or actuator_areas == {migrated_room_area}
        ):
            area_id = migrated_room_area
        elif isinstance(migrated_room_area, str) and area_names is not None:
            if migrated_room_area not in area_names:
                errors.append("the migrated Home Assistant room no longer exists")
            elif actuator_areas:
                errors.append(
                    "the controlled actuator room conflicts with the migrated room"
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
            for key in (*active_entity_fields, "power", "min_run_slots", "max_inhibit_slots", "minimum_value", "maximum_value")
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
