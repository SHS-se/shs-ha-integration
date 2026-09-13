"""Read-only device inventory and time-aligned measurement evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import isfinite
import re

if __package__:
    from .configuration_fields import _configuration_sections, _control_fields, POWER_FIELD, OPTIONAL_TEMPERATURE_FIELD
    from .operating_modes import device_mode, system_device_keys
    from .verification import observation
else:
    from configuration_fields import _configuration_sections, _control_fields, POWER_FIELD, OPTIONAL_TEMPERATURE_FIELD
    from operating_modes import device_mode, system_device_keys
    from verification import observation

SAMPLE_SECONDS = 60
MAX_SAMPLE_AGE_SECONDS = 180
MAX_REPORT_ALIGNMENT_SECONDS = 5
HOUSEHOLD = {
    "grid_import": "grid_import_w", "grid_export": "grid_export_w",
    "solar_production": "pv_w", "total_consumption": "load_w",
    "battery_charge": "battery_charge_w", "battery_discharge": "battery_discharge_w",
}


def field_entities(values, fields):
    """Only declared entity/quantity fields can introduce an entity reference."""
    result = set()
    for field in fields:
        kind = field["kind"]
        if kind not in {"entity", "entities", "quantity", "power"}:
            continue
        value = values.get(field["key"])
        for entity in value if isinstance(value, list) else [value]:
            if not isinstance(entity, str) or not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", entity):
                continue
            domains = field.get("domains") or (["sensor"] if kind in {"power", "quantity"} else [])
            if not domains or entity.split(".")[0] in domains:
                result.add(entity)
    return result


def diagnostic_inventory(devices, meters, options):
    rows = {row["key"]: {**deepcopy(row), "inventory_origin": "device_inventory"} for row in devices}
    for meter in meters:
        rows.setdefault(meter["key"], {**deepcopy(meter), "inventory_origin": "meter_inventory"})
    owners = system_device_keys(list(rows.values()), options)
    excluded = set(options.get("excluded_device_readings", []))
    for key, row in rows.items():
        identity = row.get("permission", {}).get("controller_id") or row.get("system") or owners.get(key) or "device:" + key
        row.update(controller_id=identity, mode=device_mode(options, identity), diagnostics_included=key not in excluded)
        row.setdefault("mapping", deepcopy(options.get("device_control_mappings", {}).get(key, {})))
        row.setdefault("name", key)
        row["configured_mapping"] = deepcopy(options.get("device_control_mappings", {}).get(key, {}))
    # A mapping key alone is not proof of another physical device.
    unassigned = []
    for key, mapping in options.get("device_control_mappings", {}).items():
        if key in rows or key in excluded:
            continue
        refs = field_entities(mapping, _control_fields(mapping))
        shared = [row["key"] for row in rows.values() if row["diagnostics_included"] and
                  refs & field_entities(row["mapping"], _control_fields(row["mapping"]))]
        unassigned.append({"key": key, "mapping": deepcopy(mapping), "origin": "local_mapping_only",
                           "status": "not_in_current_inventory", "shares_entities_with": shared,
                           "identity_note": "May be an alternate or stale mapping; shared entities do not establish physical identity."})
    return [row for row in rows.values() if row["diagnostics_included"]], unassigned


def observation_entities(options, devices):
    fields = [field for section in _configuration_sections() for field in section["fields"]
              if field["key"] != "excluded_device_readings"]
    entities = field_entities(options, fields)
    for device in devices:
        for key in (device["key"], device.get("statistic_id")):
            if isinstance(key, str) and re.fullmatch(r"sensor\.[a-z0-9_]+", key):
                entities.add(key)
        for field in ("mapping", "configured_mapping"):
            mapping = device.get(field, {})
            entities.update(field_entities(mapping, (*_control_fields({**device, **mapping}), POWER_FIELD, OPTIONAL_TEMPERATURE_FIELD)))
    return entities - set(options.get("excluded_device_readings", []))


def _time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed


def numeric_observation(value, captured_at, units):
    result = {"value": None, "quality": "missing"}
    if not value or value["state"] is None:
        return result
    try:
        age = (captured_at - _time(value["last_reported"])).total_seconds()
        result["age_seconds"] = age
        if age < 0 or age > MAX_SAMPLE_AGE_SECONDS:
            result["quality"] = "stale" if age >= 0 else "future_timestamp"
            return result
        number = float(value["state"])
        if not isfinite(number):
            raise ValueError("non-finite")
        unit = value["attributes"].get("unit_of_measurement")
        if unit not in units:
            result["quality"] = "unsupported_unit"
            return result
        result.update(value=number * units[unit], quality="available")
    except (TypeError, ValueError, KeyError, AttributeError):
        result["quality"] = "unavailable_or_invalid"
    return result


def counter_power(entity, sample, previous):
    """Sample-boundary energy differences, never labelled instantaneous power."""
    result = {"entity_id": entity, "average_w": None, "quality": "no_previous_sample"}
    if previous is None:
        return result
    if previous["session_id"] != sample["session_id"]:
        result["quality"] = "new_session"
        return result
    if previous["context_id"] != sample["context_id"]:
        result["quality"] = "configuration_changed"
        return result
    start, end = _time(previous["at"]), _time(sample["at"])
    seconds = (end - start).total_seconds()
    result.update(start=previous["at"], end=sample["at"], interval_seconds=seconds)
    if not 0 < seconds <= 2 * SAMPLE_SECONDS:
        result["quality"] = "sample_gap"
        return result
    old, new = (row["observations"].get(entity) for row in (previous, sample))
    readings = [numeric_observation(value, at, {"Wh": .001, "kWh": 1, "MWh": 1000})
                for value, at in ((old, start), (new, end))]
    result["source_ages_seconds"] = [r.get("age_seconds") for r in readings]
    if any(r["value"] is None for r in readings):
        result["quality"] = next(r["quality"] for r in readings if r["value"] is None)
    elif old["attributes"].get("state_class") not in {"total", "total_increasing"} or new["attributes"].get("state_class") != old["attributes"].get("state_class"):
        result["quality"] = "unsupported_counter"
    elif old["attributes"].get("last_reset") != new["attributes"].get("last_reset") or readings[1]["value"] < readings[0]["value"]:
        result["quality"] = "counter_reset"
    elif _time(new["last_reported"]) <= _time(old["last_reported"]):
        result["quality"] = "no_new_report"
    else:
        source_start, source_end = _time(old["last_reported"]), _time(new["last_reported"])
        source_seconds = (source_end - source_start).total_seconds()
        energy_w_seconds = (readings[1]["value"] - readings[0]["value"]) * 3600000
        result.update(source_start=source_start.isoformat(), source_end=source_end.isoformat(),
                      source_interval_seconds=source_seconds,
                      source_interval_average_w=energy_w_seconds / source_seconds)
        if abs(source_seconds - seconds) > MAX_REPORT_ALIGNMENT_SECONDS:
            result["quality"] = "unaligned_source_reports"
        else:
            result.update(average_w=energy_w_seconds / seconds, quality="sampled_interval_average")
    return result


def measurement_sample(options, devices, read, *, at, session_id, context_id, slot_id, plan_id, slot, previous):
    observations = {}
    for entity in sorted(observation_entities(options, devices)):
        value = observation(read(entity))
        value["attributes"] = {k: v for k, v in value["attributes"].items()
                               if k in {"unit_of_measurement", "state_class", "device_class", "last_reset"}}
        observations[entity] = value
    sample = {"at": at.isoformat(), "session_id": session_id, "context_id": context_id,
              "slot_id": slot_id, "plan_id": plan_id, "observations": observations}
    meters = set().union(*(set(options.get("entities_" + category, [])) for category in HOUSEHOLD))
    meters.update(d.get("statistic_id") or d["key"] for d in devices)
    sample["energy_intervals"] = {entity: counter_power(entity, sample, previous) for entity in sorted(meters)
                                   if entity not in options.get("excluded_device_readings", [])}
    sample["household"] = {}
    sample["interval_start"] = previous["at"] if previous and previous["session_id"] == session_id else None
    same_plan_interval = bool(previous and previous["slot_id"] == slot_id and previous["plan_id"] == plan_id and slot)
    if same_plan_interval:
        try:
            start = _time(slot["start"])
            same_plan_interval = start <= _time(previous["at"]) < at <= start + timedelta(minutes=15)
        except (ValueError, KeyError, TypeError, AttributeError):
            same_plan_interval = False
    for category, forecast in HOUSEHOLD.items():
        sources = options.get("entities_" + category, [])
        intervals = [sample["energy_intervals"].get(entity, {}) for entity in sources]
        available = bool(sources) and all(row.get("average_w") is not None for row in intervals)
        average = sum(row["average_w"] for row in intervals) if available else None
        source_interval_in_slot = available and same_plan_interval and all(
            _time(row["source_start"]) >= _time(slot["start"]) for row in intervals)
        planned = slot.get(forecast) if source_interval_in_slot else None
        sample["household"][category] = {"sources": list(sources), "average_w": average,
            "quality": "sampled_interval_average" if available else "incomplete_sources" if sources else "not_configured",
            "planned_w": planned, "difference_w": average - planned if average is not None and planned is not None else None}
    sample["plan_comparison_available"] = same_plan_interval and any(v["difference_w"] is not None for v in sample["household"].values())
    sample["device_power"] = {d["key"]: {"entity_id": d.get("mapping", {}).get("power"),
        **numeric_observation(observations.get(d.get("mapping", {}).get("power")), at, {"W": 1, "kW": 1000})}
        for d in devices}
    for device in devices:
        power = sample["device_power"][device["key"]]
        power["unit"] = "W"
        if power["entity_id"] not in observations:
            power["quality"] = "not_configured_as_measurement"
    for field in ("grid_export_power_entity", "battery_power_measurement_entity"):
        sample.setdefault("instantaneous_power", {})[field] = {"entity_id": options.get(field), "unit": "W",
            **numeric_observation(observations.get(options.get(field)), at, {"W": 1, "kW": 1000})}
    return sample
