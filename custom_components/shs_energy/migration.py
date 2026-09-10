"""One-time import of old option formats into the current persisted schema."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

if __package__:
    from .configuration_schema import MAPPING_KEYS, OPTION_KEYS, PERSISTED_KEYS, ROOM_AREA_FIELD
else:
    from configuration_schema import MAPPING_KEYS, OPTION_KEYS, PERSISTED_KEYS, ROOM_AREA_FIELD

ARCHIVE_KEY = "_legacy_configuration_archive"
EV_FIELDS = {
    "ev_connected_entity": "connected_entity_id",
    "ev_soc_entity": "soc_entity_id",
    "ev_target_soc_entity": "target_soc_entity_id",
    "ev_departure_entity": "departure_entity_id",
    "ev_energy_remaining_entity": "energy_remaining_entity_id",
    "ev_phase_count": "phase_count",
    "ev_phase_voltage": "voltage",
    "ev_charge_efficiency": "charge_efficiency",
}


def mapped_entity_ids(options: dict[str, Any]) -> set[str]:
    """Entities whose registry areas can supply a current room association."""
    mappings = options.get("device_control_mappings")
    if not isinstance(mappings, dict):
        return set()
    return {
        entity_id
        for mapping in mappings.values()
        if isinstance(mapping, dict)
        for key, value in mapping.items()
        if key.endswith("_entity_id") or key.endswith("_entity_ids")
        for entity_id in (value if isinstance(value, list) else [value])
        if isinstance(entity_id, str) and entity_id
    }


def migrate_options(
    options: dict[str, Any],
    *,
    entity_area_ids: dict[str, str] | None = None,
    source_version: int | None = None,
) -> tuple[dict[str, Any], bool]:
    """Import needed values once, remove old representations, and report names.

    Explicit current values win, including zero and false. Hardware bounds are
    not inferred from live states: a missing reviewed limit stays missing.
    No archive, defaults, credentials, or command journal is written here.
    """
    result = {key: deepcopy(value) for key, value in options.items() if key in PERSISTED_KEYS}
    prior = options.get("_migration_report", {})
    report = {
        kind: set(prior.get(kind, [])) if isinstance(prior, dict) else set()
        for kind in ("imported", "removed", "needs_attention")
    }
    report["removed"].update(set(options) - PERSISTED_KEYS)
    if source_version in (5, 6):
        # These migrations deleted settings rather than archiving their values.
        # Retain the removal evidence, but replace instructions for the retired
        # interface with the actual setup work required by the restored schema.
        report["needs_attention"].difference_update({
            "battery_dispatch: prior permission requires new binding and commissioning",
            "pool_service: customer request interface and commissioning required",
        })
        mappings = options.get("device_control_mappings", {})
        for path in report["removed"]:
            if path.startswith("device_control_mappings."):
                source = path.removeprefix("device_control_mappings.")
                if source in options.get("entities_pool_heating", []) and source not in mappings:
                    report["needs_attention"].add(path)
            elif path in OPTION_KEYS and path.startswith(("pool_", "battery_")) and path not in options:
                report["needs_attention"].add(path)
        # Permissions from the abandoned interfaces cannot authorize the
        # restored direct controllers. Entity choices must be reviewed again.
        result["pool_control_enabled"] = False
        result["battery_control_enabled"] = False
    archive = options.get(ARCHIVE_KEY, {})
    archive = archive if isinstance(archive, dict) else {}
    for key in ("ev_phase_count", "ev_charge_efficiency"):
        if key not in result and key in archive:
            result[key] = deepcopy(archive[key])
            report["imported"].add(key)
    if "ev_phase_voltage" not in result and "ev_voltage" in options:
        result["ev_phase_voltage"] = options["ev_voltage"]
        report["imported"].add("ev_phase_voltage")

    raw_mappings = options.get("device_control_mappings", {})
    if not isinstance(raw_mappings, dict):
        raw_mappings = {}
        report["needs_attention"].add("device_control_mappings")
    # Convert EV observations before dropping their former card fields.
    ev_mappings = {
        key: mapping for key, mapping in raw_mappings.items()
        if isinstance(mapping, dict) and (
            mapping.get("control_type") == "current_limit"
            or (mapping.get("control_type") == "variable_power" and (
                key in options.get("entities_ev_charging", [])
                or "connected_entity_id" in mapping
                or "soc_entity_id" in mapping
                or (options.get("ev_charge_current_entity") is not None
                    and mapping.get("control_entity_id") == options["ev_charge_current_entity"])
            ))
        )
    }
    for current, old in EV_FIELDS.items():
        if current in result:
            continue
        candidates = [m[old] for m in ev_mappings.values() if old in m]
        if candidates and all(value == candidates[0] for value in candidates):
            result[current] = deepcopy(candidates[0])
            report["imported"].add(current)
        elif candidates:
            report["needs_attention"].add(current)

    mappings = {}
    for key, raw in raw_mappings.items():
        path = f"device_control_mappings.{key}"
        if not isinstance(raw, dict):
            mappings[key] = {}
            report["needs_attention"].add(path)
            continue
        draft = deepcopy(raw)
        if draft.get("control_type") == "current_limit":
            draft["control_type"] = "variable_power"
            report["imported"].add(f"{path}.control_type")

        def copy_first(target, *sources):
            if target in draft:
                return
            for source in sources:
                if source in raw:
                    draft[target] = deepcopy(raw[source])
                    report["imported"].add(f"{path}.{target}")
                    return

        copy_first("power", "power_entity_id", "power_w")
        copy_first(ROOM_AREA_FIELD, "_migrated_room_area_id", "area_id")
        if draft.get("control_type") == "variable_power":
            copy_first("control_entity_id", "current_control_entity_id", "power_control_entity_id")
            copy_first("minimum_value", "min_current_a")
            copy_first("maximum_value", "max_current_a")
            if key in ev_mappings:
                for target, source in (("control_entity_id", "ev_charge_current_entity"),
                                       ("minimum_value", "ev_min_current_a"),
                                       ("maximum_value", "ev_max_current_a")):
                    if target not in draft and source in options:
                        draft[target] = deepcopy(options[source])
                        report["imported"].add(f"{path}.{target}")
        if ROOM_AREA_FIELD not in draft and (
            draft.get("control_type") == "setpoint" or "temperature_entity_id" in draft
        ):
            areas = {(entity_area_ids or {}).get(e) for e in draft.get("actuator_entity_ids", [])}
            areas.discard(None)
            if len(areas) == 1:
                draft[ROOM_AREA_FIELD] = areas.pop()
                report["imported"].add(f"{path}.{ROOM_AREA_FIELD}")
            else:
                report["needs_attention"].add(f"{path}.{ROOM_AREA_FIELD}")
        allowed = MAPPING_KEYS.get(draft.get("control_type"), {"control_type"})
        mappings[key] = {field: value for field, value in draft.items() if field in allowed}
        report["removed"].update(f"{path}.{field}" for field in set(raw) - allowed)
        required = {
            "variable_power": ("control_entity_id", "minimum_value", "maximum_value"),
            "permit_inhibit": ("actuator_entity_ids", "max_inhibit_slots"),
            "setpoint": ("actuator_entity_ids", "temperature_entity_id"),
            "switch_schedule": ("actuator_entity_ids",),
        }.get(draft.get("control_type"))
        if required is None:
            report["needs_attention"].add(f"{path}.control_type")
        else:
            report["needs_attention"].update(f"{path}.{field}" for field in required if field not in draft
                and not (field == "temperature_entity_id" and result.get("rooms", {}).get(draft.get(ROOM_AREA_FIELD), {}).get(field)))
    rooms = deepcopy(result.get("rooms", {}))
    sources = {}
    for key, mapping in mappings.items():
        room = mapping.get(ROOM_AREA_FIELD)
        if room and "temperature_entity_id" in mapping:
            sources.setdefault(room, set()).add(mapping.pop("temperature_entity_id"))
            report["removed"].add(f"device_control_mappings.{key}.temperature_entity_id")
    for room, candidates in sources.items():
        if room in rooms:
            continue
        if len(candidates) != 1:
            raise ValueError(f"Room {room} has conflicting temperature sources; select one before upgrading")
        rooms[room] = {"temperature_entity_id": candidates.pop()}
        report["imported"].add(f"rooms.{room}.temperature_entity_id")
    if rooms:
        result["rooms"] = rooms
    if "device_control_mappings" in options:
        result["device_control_mappings"] = mappings
    evidence = result.get("discovery_evidence")
    if isinstance(evidence, dict):
        result["discovery_evidence"] = {key: value for key, value in evidence.items() if key in OPTION_KEYS}
        report["removed"].update(f"discovery_evidence.{key}" for key in set(evidence) - OPTION_KEYS)
    if any(report.values()):
        result["_migration_report"] = {kind: sorted(names) for kind, names in report.items()}
    return result, result != options
