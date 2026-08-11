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
    "current_limit",
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
        direct = _text(mapping, "setpoint_entity_id")
        scheduled = _text(mapping, "comfort_high_entity_id") and _text(
            mapping, "comfort_low_entity_id"
        )
        if not direct and not scheduled:
            errors.append(
                "a setpoint entity or both high and low comfort entities are required"
            )
    elif control_type == "permit_inhibit":
        if not _entities(mapping, "actuator_entity_ids"):
            errors.append("at least one permit/inhibit actuator is required")
        if not _positive_number(mapping, "max_inhibit_slots"):
            errors.append("maximum inhibit slots must be positive")
    elif control_type == "switch_schedule":
        if not _entities(mapping, "actuator_entity_ids"):
            errors.append("at least one switch actuator is required")
        if not _positive_number(mapping, "min_run_slots"):
            errors.append("minimum run slots must be positive")
    elif control_type == "variable_power":
        if not _text(mapping, "power_control_entity_id"):
            errors.append("power control entity is required")
    elif control_type == "current_limit":
        for key, label in (
            ("current_control_entity_id", "charge current control entity"),
            ("connected_entity_id", "connected entity"),
            ("soc_entity_id", "vehicle state of charge entity"),
            ("target_soc_entity_id", "target state of charge entity"),
        ):
            if not _text(mapping, key):
                errors.append(f"{label} is required")
        if not _positive_number(mapping, "battery_capacity_kwh"):
            errors.append("vehicle usable battery capacity must be positive")
        for key, label in (
            ("min_current_a", "minimum charge current"),
            ("max_current_a", "maximum charge current"),
            ("current_step_a", "charge current step"),
            ("phase_count", "phase count"),
            ("voltage", "charging voltage"),
            ("min_run_slots", "minimum run slots"),
        ):
            if not _positive_number(mapping, key):
                errors.append(f"{label} must be positive")
        if (
            _positive_number(mapping, "min_current_a")
            and _positive_number(mapping, "max_current_a")
            and float(mapping["min_current_a"]) > float(mapping["max_current_a"])
        ):
            errors.append("minimum charge current cannot exceed maximum charge current")
    return errors


def mapping_report(
    requested_control_type: str | None,
    mapping: dict[str, Any] | None,
    known_entity_ids: set[str] | None = None,
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
    if known_entity_ids is not None and not configured_entity_ids.issubset(
        known_entity_ids
    ):
        errors.append("one or more configured entities no longer exist")
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
        )
        device["load_type"] = configuration.get(
            "load_type", device["suggested_load_type"]
        )
        ready = requested_role == "controllable" and report["mapping_status"] == "ready"
        device["planning_role"] = "controllable" if ready else "base_load"
        device["control_type"] = requested_control if ready else None
        device.update(report)
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
