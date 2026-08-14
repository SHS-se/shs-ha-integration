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


def mapping_errors(mapping: dict[str, Any], control_type: str) -> list[str]:
    """Return structural mapping errors for one control contract."""
    if control_type not in CONTROL_TYPES:
        return ["unsupported control type"]
    if mapping.get("control_type") != control_type:
        return ["the saved mapping belongs to a different control type"]

    errors: list[str] = []
    if control_type == "setpoint":
        if not _text(mapping, "temperature_entity_id"):
            errors.append("room temperature entity is required")
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
    errors = mapping_errors(mapping, requested_control_type)
    configured_entity_ids = {
        entity_id
        for key, value in mapping.items()
        if key.endswith("_entity_id") or key.endswith("_entity_ids")
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
    if requested_control_type == "setpoint":
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
        if missing_areas:
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
    entity_count = sum(
        len(value) if isinstance(value, list) else 1
        for key, value in mapping.items()
        if (key.endswith("_entity_id") or key.endswith("_entity_ids")) and value
    )
    summary = {
        "control_type": requested_control_type,
        "entity_count": entity_count,
        "configured_fields": sorted(
            key for key, value in mapping.items() if key != "control_type" and value
        ),
    }
    if requested_control_type == "setpoint" and isinstance(area_id, str):
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
        report = mapping_report(
            requested_control if requested_role == "controllable" else None,
            mappings.get(device["key"]),
            known_entity_ids,
            entity_names,
            area_names,
            entity_area_ids,
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
