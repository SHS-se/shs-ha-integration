"""Live battery input capture. Reading an entity never commissions its physics."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from math import isfinite

if __package__:
    from .battery_supply import SupplyScope, observe_supply
else:
    from battery_supply import SupplyScope, observe_supply


POWER_OPTIONS = ("house_consumption_power_entity", "solar_production_power_entity", "battery_power_measurement_entity")
CONTROL_OPTIONS = ("battery_mode_entity", "battery_charge_limit_entity", "battery_discharge_limit_entity")
# Diagnostics only; an admitted measurement profile owns its own timing budget.
DIAGNOSTIC_AGE_MS = 30_000


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("missing report timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("report timestamp has no timezone")
    return int(stamp.timestamp() * 1000)


def _finite(value):
    value = float(value)
    if not isfinite(value):
        raise ValueError("nonfinite measurement")
    return value


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def planned_power_bindings(options, devices):
    excluded = set(options.get("excluded_device_readings", []))
    mappings = options.get("device_control_mappings", {})
    result = {}
    for device in devices:
        key = device["key"]
        if key in excluded or device.get("planning_role") != "controllable":
            continue
        if key in result:
            raise ValueError("duplicate Planned device")
        result[key] = mappings.get(key, {}).get("power")
    return dict(sorted(result.items()))


def source_revision(options, devices):
    """Role revision and exact configured source identities, including Verification."""
    return _hash({"sources": {key: options.get(key) for key in POWER_OPTIONS},
                  "planned": planned_power_bindings(options, devices),
                  "roles": sorted((d["key"], d.get("planning_role"), d.get("planning_choice_at"))
                                  for d in devices if d["key"] not in options.get("excluded_device_readings", []))})


@dataclass(frozen=True)
class MeasurementProfile:
    """Reviewed installation data, never inferred from names or shared device IDs.

    This attests only the AC power partition. It gives no native response, meter
    counter, transition or writer authority. Installation tools must retain their
    evidence separately; a free-form evidence label is not a commissioning test.
    """
    revision: str
    source_revision: str
    boundary: str
    evidence_reference: str
    max_age_ms: int
    max_alignment_ms: int
    source_entities: tuple[str, ...]

    @classmethod
    def read(cls, value):
        fields = {"schema", "revision", "source_revision", "boundary", "evidence_reference", "max_age_ms", "max_alignment_ms", "source_entities"}
        if not isinstance(value, dict) or set(value) != fields or value["schema"] != "battery-measurements-v1":
            raise ValueError("unsupported measurement profile")
        for key in ("revision", "source_revision", "boundary", "evidence_reference"):
            if type(value[key]) is not str or not 0 < len(value[key]) <= 128:
                raise ValueError("measurement profile requires bounded provenance")
        age, alignment = value["max_age_ms"], value["max_alignment_ms"]
        if type(age) is not int or type(alignment) is not int or not 0 <= alignment <= age <= 120_000 or age == 0:
            raise ValueError("invalid measurement timing profile")
        entities = value["source_entities"]
        if (type(entities) is not list or not 2 <= len(entities) <= 34 or
                any(type(e) is not str or not e.startswith("sensor.") for e in entities) or
                entities != sorted(set(entities))):
            raise ValueError("measurement profile requires disjoint canonical power sources")
        return cls(*(value[k] for k in ("revision", "source_revision", "boundary", "evidence_reference", "max_age_ms", "max_alignment_ms")), tuple(entities))


def native_surface(options, reports):
    """Inspect interface units/resolution. This deliberately makes no AC/DC claim."""
    entities = [options.get(key) for key in CONTROL_OPTIONS]
    if any(not isinstance(e, str) for e in entities) or len(set(entities)) != 3:
        raise ValueError("three distinct battery control entities are required")
    mode, charge, discharge = entities
    state = reports.get(mode)
    choices = state and state.get("attributes", {}).get("options")
    if not mode.startswith("select.") or not isinstance(choices, list) or state.get("state") not in choices:
        raise ValueError("battery mode readback or choices are unavailable")
    surface = {"mode_entity": mode, "mode_options": list(choices), "limits": {}}
    readback = {"mode": state["state"], "mode_reported_at_ms": _stamp(state.get("last_reported"))}
    for key, entity in (("charge", charge), ("discharge", discharge)):
        value = reports.get(entity)
        attributes = value and value.get("attributes", {})
        if not entity.startswith("number.") or not attributes or attributes.get("unit_of_measurement") not in ("W", "kW"):
            raise ValueError(f"{key}: a native W or kW number is required")
        scale = Decimal(1000 if attributes["unit_of_measurement"] == "kW" else 1)
        try:
            low, high, quantum, watts = (Decimal(str(x)) * scale for x in
                                        (attributes["min"], attributes["max"], attributes["step"], value["state"]))
            if not all(x.is_finite() for x in (low, high, quantum, watts)) or not 0 <= low <= watts <= high or quantum <= 0:
                raise ValueError("invalid native range")
            if (watts - low) % quantum:
                raise ValueError("readback is not representable by its native step")
        except (InvalidOperation, KeyError, TypeError) as error:
            raise ValueError(f"{key}: invalid native metadata") from error
        surface["limits"][key] = {"entity_id": entity, "unit": attributes["unit_of_measurement"],
                                  "minimum_w": float(low), "maximum_w": float(high), "quantum_w": float(quantum)}
        readback[key + "_limit_w"] = float(watts)
        readback[key + "_reported_at_ms"] = _stamp(value.get("last_reported"))
    return {**surface, "revision": _hash(surface), "readback": readback,
            "power_basis": "unverified", "response_commissioned": False}


def capture_battery_inputs(options, devices, read_entity, *, now_ms, profile=None, scope=None):
    """Copy each state once in a synchronous capture; preserve every report time.

    The same-turn read is not a claim that independent meters sampled together.
    Power, SOC and native limits remain inspectable even when accounting is blocked.
    """
    planned = planned_power_bindings(options, devices)
    bindings = {**{key: options.get(key) for key in POWER_OPTIONS},
                **{"planned:" + key: entity for key, entity in planned.items()},
                **{key: options.get(key) for key in CONTROL_OPTIONS},
                "battery_soc_entity": options.get("battery_soc_entity")}
    reports = {entity: deepcopy(read_entity(entity)) for entity in sorted({e for e in bindings.values() if isinstance(e, str)})}
    sources, blockers = {}, []
    for role, entity in bindings.items():
        value = reports.get(entity) if isinstance(entity, str) else None
        row = {"entity_id": entity if isinstance(entity, str) else None}
        try:
            if not isinstance(value, dict):
                raise ValueError("source_not_configured" if not isinstance(entity, str) else "source_unavailable")
            reported = _stamp(value.get("last_reported"))
            row.update(reported_at_ms=reported, age_ms=now_ms - reported)
            if reported > now_ms:
                raise ValueError("future_report")
            attributes = value.get("attributes", {})
            if role not in CONTROL_OPTIONS:
                unit = attributes.get("unit_of_measurement")
                row["unit"] = unit
                watts = _finite(value["state"])
                if role == "battery_soc_entity":
                    if unit != "%" or not 0 <= watts <= 100:
                        raise ValueError("percentage_soc_required")
                    row["soc_fraction"] = watts / 100
                else:
                    if unit not in ("W", "kW") or attributes.get("state_class") != "measurement":
                        raise ValueError("instantaneous_power_required")
                    watts *= 1000 if unit == "kW" else 1
                    if watts < 0 and role != "battery_power_measurement_entity":
                        raise ValueError("negative_consumption_or_pv")
                    if not isfinite(watts):
                        raise ValueError("nonfinite power after unit conversion")
                    row["watts"] = watts
            row["state"] = "reported" if now_ms - reported < DIAGNOSTIC_AGE_MS else "stale_report"
            if row["state"] != "reported":
                blockers.append({"source": role, "reason": row["state"]})
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            row.update(state="unavailable", reason=str(error))
            blockers.append({"source": role, "reason": str(error)})
        sources[role] = row
    revision = source_revision(options, devices)
    accounting = None
    try:
        surface = native_surface(options, reports)
    except (ValueError, TypeError) as error:
        surface = {"state": "unavailable", "reason": str(error), "response_commissioned": False}
    if profile is None:
        blockers.append({"source": "measurement_boundary", "reason": "ac_boundary_unverified"})
    else:
        try:
            if profile.source_revision != revision:
                raise ValueError("measurement_profile_configuration_changed")
            if not isinstance(scope, SupplyScope):
                raise ValueError("explicit_supply_scope_required")
            required = {options.get("house_consumption_power_entity"), options.get("solar_production_power_entity")}
            keys = set(planned) if scope.include_base else set(scope.planned_device_keys)
            required.update(planned.get(key) for key in keys)
            if not required <= set(profile.source_entities):
                raise ValueError("unreviewed_power_source")
            result, evidence = observe_supply(scope, options, devices, reports.get, at_ms=now_ms,
                max_age_ms=profile.max_age_ms, max_alignment_ms=profile.max_alignment_ms, boundary=profile.boundary,
                membership_revision=revision, expected_membership_revision=profile.source_revision)
            accounting = {**asdict(result), "valid_until_ms": min(r.valid_until_ms for r in evidence),
                          "measurement_revision": profile.revision, "supply_scope": asdict(scope)}
        except (ValueError, TypeError) as error:
            blockers.append({"source": "supply_accounting", "reason": str(error)})
    blockers.append({"source": "native_response", "reason": "native_response_model_uncommissioned"})
    return {"schema": "battery-live-inputs-v1", "at_ms": now_ms, "source_revision": revision,
            "sources": sources, "native_surface": surface, "accounting": accounting,
            "blockers": blockers, "control_authority": False}


class BatteryLiveInputs:
    """Capture owner with fail-closed replacement and a bounded diagnostic journal."""
    def __init__(self, read_entity, now_ms, persist):
        self._read_entity, self._now_ms, self._persist = read_entity, now_ms, persist
        self._lock = asyncio.Lock()
        self._closed = False
        self._latest = {"state": "not_sampled", "control_authority": False}
        self._last_save_ms = None

    def snapshot(self):
        value = deepcopy(self._latest)
        if "at_ms" in value:
            value["capture_age_ms"] = self._now_ms() - value["at_ms"]
            value["capture_stale"] = value["capture_age_ms"] >= DIAGNOSTIC_AGE_MS
        return value

    async def sample(self, options, devices, *, profile=None, scope=None):
        async with self._lock:
            await self._sample(options, devices, profile=profile, scope=scope)

    async def _sample(self, options, devices, *, profile=None, scope=None):
        if self._closed:
            return
        # No await occurs between reading configuration, time, and state table.
        now = self._now_ms()
        try:
            value = capture_battery_inputs(options, devices, self._read_entity, now_ms=now, profile=profile, scope=scope)
        except (ValueError, TypeError) as error:
            self._latest = {"state": "unavailable", "reason": str(error), "control_authority": False}
            return
        self._latest = value
        if self._last_save_ms is None or now - self._last_save_ms >= 60_000:
            try:
                await self._persist(value)
                self._last_save_ms = now
            except Exception as error:
                if not self._closed:
                    self._latest = {**value, "persistence_error": type(error).__name__}

    def unavailable(self, reason):
        if not self._closed:
            self._latest = {"state": "unavailable", "reason": reason, "control_authority": False}

    def close(self):
        self._closed = True
        self._latest = {"state": "stopped", "control_authority": False}
