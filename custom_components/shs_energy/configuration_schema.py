"""Current persisted fields and strict save contracts, independent of runtime IO."""

from copy import deepcopy

from math import isfinite
import re
from typing import Any
if __package__:
    from .configuration_values import resolve_quantity
    from .device_commands import execution_setup_errors
    from . import const as c
    from .configuration_fields import _configuration_sections, _control_fields, section_fields, CONTROL_FIELDS
    from .device_controls import mapping_report, is_room_thermal_control, battery_control_errors, pool_band_errors
else:
    from configuration_values import resolve_quantity
    from device_commands import execution_setup_errors
    import const as c
    from configuration_fields import _configuration_sections, _control_fields, section_fields, CONTROL_FIELDS
    from device_controls import mapping_report, is_room_thermal_control, battery_control_errors, pool_band_errors

OPTION_FIELDS = {
    field["key"]: field
    for section in _configuration_sections()
    for field in section_fields(section)
}
# Internal scheduling resolution is fixed, not another editable setting.
OPTION_KEYS = frozenset(OPTION_FIELDS)
METADATA_KEYS = frozenset({"configuration_reviewed_at", "discovery_evidence", "_migration_report"})
PERSISTED_KEYS = OPTION_KEYS | METADATA_KEYS | {"device_control_mappings", "rooms", "automatic_setup", "forecast_resolution_minutes", "device_modes"}
ROOM_AREA_FIELD = c.ROOM_AREA_FIELD
MAPPING_KEYS = {
    kind: {field["key"] for field in fields} | {"control_type", ROOM_AREA_FIELD}
    for kind, fields in CONTROL_FIELDS.items()
}
MAPPING_KEYS["switch_schedule"].add("temperature_entity_id")


def validate_mapping_keys(mapping):
    control_type = mapping.get("control_type")
    if control_type not in MAPPING_KEYS:
        raise ValueError("unsupported control type")
    unknown = set(mapping) - MAPPING_KEYS[control_type]
    if unknown:
        raise ValueError("unknown device fields: " + ", ".join(sorted(unknown)))


def merge_options(existing, incoming):
    """Apply only current public settings; never import old fields on save."""
    unknown = set(incoming) - OPTION_KEYS
    if unknown:
        raise ValueError("unknown configuration keys: " + ", ".join(sorted(unknown)))
    result = deepcopy(existing)
    result.update(deepcopy(incoming))
    return result


def configuration_defaults(latitude: float, longitude: float) -> dict[str, Any]:
    """Defaults with product meaning, shared by the UI and runtime."""
    return {
        c.OPT_PLANNING_MODE: c.DEFAULT_PLANNING_MODE,
        c.OPT_AUTOMATIC_SETUP: True,
        c.OPT_EV_CONTROL_ENABLED: False,
        c.OPT_POOL_CONTROL_ENABLED: False,
        c.OPT_DEVICE_CONTROL_MAPPINGS: {},
        "rooms": {},
        c.OPT_FORECAST_RESOLUTION_MINUTES: c.DEFAULT_FORECAST_RESOLUTION_MINUTES,
        c.OPT_PV_FORECAST_LATITUDE: latitude,
        c.OPT_PV_FORECAST_LONGITUDE: longitude,
        # Every store is part of the home until someone says otherwise, so an
        # installation that predates these keys is unchanged by them.
        c.OPT_BATTERY_ENABLED: True,
        c.OPT_POOL_ENABLED: True,
        c.OPT_EV_ENABLED: True,
        c.OPT_BATTERY_MIN_SOC: 0.05,
        c.OPT_BATTERY_TARGET_SOC: 0.8,
        # A target is a preference by default. Making 80% hard can force a
        # flexible load onto night import while solar is reserved for storage.
        c.OPT_BATTERY_TARGET_IS_HARD: False,
        c.OPT_BATTERY_CHARGE_EFFICIENCY: 0.95,
        c.OPT_BATTERY_DISCHARGE_EFFICIENCY: 0.95,
        # Export from storage is an explicit customer preference. It is
        # advisory until a separately reviewed battery executor exists.
        c.OPT_BATTERY_EXPORT_ENABLED: False,
        c.OPT_BATTERY_EXPORT_RESERVE_SOC: 0.8,
        c.OPT_BATTERY_EXPORT_MIN_PRICE: 2.5,
        # Commanding the battery stays off until the response, sign and
        # confirmation behaviour have been measured on the installation. A
        # discovered entity is an offer to configure, never an authorisation.
        c.OPT_BATTERY_CONTROL_ENABLED: False,
        c.OPT_TERMINAL_SOC_MIN: 0.2,
        c.OPT_TERMINAL_ENERGY_VALUE: 1.0,
        c.OPT_EV_CHARGE_EFFICIENCY: c.EV_CHARGE_EFFICIENCY,
        c.OPT_EV_KWH_PER_KM: c.DEFAULT_EV_KWH_PER_KM,
    }



def normalise_field_value(
    read_entity,
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

    if kind == "quantity":
        scale = field.get("scale") or 1
        number = resolve_quantity(value, read_entity, unit=field["unit"],
            minimum=field["minimum"] / scale if "minimum" in field else None,
            maximum=field["maximum"] / scale if "maximum" in field else None,
            label=f"{context}: {label}")
        return value.strip() if isinstance(value, str) and value.strip().startswith("sensor.") else number

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
        state = read_entity(text)
        if state is not None:
            unit = state["attributes"].get("unit_of_measurement")
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
        if kind == "entity" and not isinstance(value, str):
            raise ValueError(f"{context}: {label} must be one entity")
        values = value if isinstance(value, list) else [value]
        if kind == "entities" and not isinstance(value, list):
            raise ValueError(f"{context}: {label} must be a list of entities")
        if field.get("max_items") is not None and len(values) > field["max_items"]:
            raise ValueError(f"{context}: {label} must be one entity")
        normalised = []
        for raw_entity_id in values:
            if not isinstance(raw_entity_id, str) or not raw_entity_id.strip():
                raise ValueError(f"{context}: {label} contains an invalid entity")
            entity_id = raw_entity_id.strip()
            state = read_entity(entity_id)
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




def resolve_configuration(options, latitude=0.0, longitude=0.0):
    """Produce a detached runtime view from current records and shared defaults."""
    resolved = configuration_defaults(latitude, longitude)
    resolved.update(deepcopy(options))
    if __package__:
        from .operating_modes import device_mode
    else:
        from operating_modes import device_mode
    modes = resolved.get("device_modes", {})
    resolved["planning_mode"] = "live" if any(mode != "monitoring" for mode in modes.values()) else "disabled"
    for system in ("battery", "pool", "ev"):
        resolved[system + "_control_enabled"] = device_mode(resolved, system) == "controlling"
    rooms = resolved["rooms"]
    for mapping in resolved["device_control_mappings"].values():
        room_id = mapping.get(ROOM_AREA_FIELD)
        if room_id:
            # A room has one observation source; never consult a second copy.
            mapping.pop("temperature_entity_id", None)
            source = rooms.get(room_id, {}).get("temperature_entity_id")
            if source is not None:
                mapping["temperature_entity_id"] = source
    return resolved


def prepare_options(existing, incoming, read_entity, *, latitude=0.0, longitude=0.0):
    """Validate a public patch without resaving derived defaults or room views."""
    result = merge_options(existing, incoming)
    for key, value in incoming.items():
        value = normalise_field_value(read_entity, OPTION_FIELDS[key], value, context="Configuration")
        # A cleared setting remains explicitly empty rather than regaining a default.
        result[key] = value
    current = resolve_configuration(result, latitude, longitude)
    if "battery_min_soc" in incoming and current.get("battery_min_soc") is not None:
        minimum = resolve_quantity(current["battery_min_soc"], read_entity, unit="%", minimum=0, maximum=1, label="Minimum charge")
        if minimum >= 1:
            raise ValueError("Minimum charge must be below 100%")
    mode_keys = [key for key, field in OPTION_FIELDS.items() if field["kind"] == "battery_mode"]
    if "battery_mode_entity" in incoming or any(key in incoming for key in mode_keys):
        mode = read_entity(current.get("battery_mode_entity")) if current.get("battery_mode_entity") else None
        for key in mode_keys:
            if current.get(key) and (mode is None or current[key] not in mode["attributes"].get("options", [])):
                raise ValueError(f"{OPTION_FIELDS[key]['label']}: choose an option from the control-mode entity")
    if current["battery_control_enabled"]:
        errors = battery_control_errors(current)
        if errors:
            raise ValueError("Battery control: " + "; ".join(errors))
    if current["pool_control_enabled"]:
        errors = pool_band_errors(current)
        if errors:
            raise ValueError("Pool control: " + "; ".join(errors))
    if current["ev_control_enabled"] and not current.get("ev_charge_switch_entity"):
        raise ValueError("EV charging start/stop switch is required for control")
    return result


def save_device(existing, key, submitted, device, read_entity, *, entity_names, area_names, entity_area_ids):
    """Save one device and its shared room source; unrelated records survive."""
    result = deepcopy(existing)
    stored = result.setdefault("device_control_mappings", {})
    if submitted is None:
        stored.pop(key, None)
        return result
    validate_mapping_keys(submitted)
    kind = device["control_type"]
    if submitted.get("control_type") != kind:
        raise ValueError(f"{device['name']}: configuration belongs to a different control type")
    mapping = {"control_type": kind}
    if stored.get(key, {}).get(ROOM_AREA_FIELD):
        mapping[ROOM_AREA_FIELD] = stored[key][ROOM_AREA_FIELD]
    for field in _control_fields(device):
        value = normalise_field_value(read_entity, field, submitted.get(field["key"]), context=device["name"])
        if value is not None or (field["key"] in submitted and submitted[field["key"]] is None):
            mapping[field["key"]] = value
    if resolve_configuration(existing).get("device_modes", {}).get(key) in ("controlling", "control_verification"):
        errors = execution_setup_errors(mapping)
        if errors:
            raise ValueError(f"{device['name']}: " + "; ".join(errors))
    report = mapping_report(kind, mapping, set(entity_names), entity_names, area_names, entity_area_ids,
                            room_control=is_room_thermal_control(kind, device.get("category")))
    if report["mapping_status"] != "ready":
        raise ValueError(f"{device['name']}: {report['mapping_error']}")
    room = report["mapping_summary"].get("room_key")
    if room:
        mapping[ROOM_AREA_FIELD] = room
        source = mapping.pop("temperature_entity_id")
        result.setdefault("rooms", {})[room] = {"temperature_entity_id": source}
    else:
        mapping.pop(ROOM_AREA_FIELD, None)
    stored[key] = mapping
    return result


def shared_devices(devices, options):
    excluded = set(options.get("excluded_device_readings", []))
    return [device for device in devices if device["key"] not in excluded]
