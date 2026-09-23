"""Readings each planned device needs, checked before the snapshot is built.

User requirement, 23 September 2026 (see docs/authoritative-plan-contract.md):
an impossible sensor reading must affect only its device. A reading that is
unavailable, not a number or physically impossible switches that store off for
this snapshot exactly as the customer's own store switch would, and is named in
the snapshot's ``measurement_issues`` so the plan and both interfaces can say
why. Every other device is planned as usual.

Realistic state is not an issue: a car above its charge limit, an empty car, a
pack below a raised cut-off and a pool warmer than its stop temperature are all
planned as they are. A missing entity or a sensor with the wrong unit is a
configuration error and keeps its setup correction in the snapshot builder.

The ranges are physical and match the planner's own isolation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from math import isfinite
from typing import Any, Callable

try:  # pragma: no cover - exercised by both import paths
    from .configuration_values import BATTERY_QUANTITIES, QUANTITY_UNITS
    from .const import (
        OPT_BATTERY_SOC_ENTITY,
        OPT_EV_CONNECTED_ENTITY,
        OPT_EV_ENERGY_REMAINING_ENTITY,
        OPT_EV_SOC_ENTITY,
        OPT_EV_TARGET_SOC_ENTITY,
        OPT_POOL_WATER_TEMPERATURE_ENTITY,
    )
    from .device_controls import mapped_planning_path
except ImportError:  # The test suite imports flat modules without Home Assistant.
    from configuration_values import BATTERY_QUANTITIES, QUANTITY_UNITS  # type: ignore[no-redef]
    from const import (  # type: ignore[no-redef]
        OPT_BATTERY_SOC_ENTITY,
        OPT_EV_CONNECTED_ENTITY,
        OPT_EV_ENERGY_REMAINING_ENTITY,
        OPT_EV_SOC_ENTITY,
        OPT_EV_TARGET_SOC_ENTITY,
        OPT_POOL_WATER_TEMPERATURE_ENTITY,
    )
    from device_controls import mapped_planning_path  # type: ignore[no-redef]

UNREADABLE = frozenset({"unknown", "unavailable"})
# The age the snapshot already required of the battery's state of charge.
BATTERY_SOC_MAX_AGE = timedelta(minutes=15)
POOL_WATER_RANGE_C = (-5.0, 60.0)
_NAMES = {"battery": "The home battery", "ev": "The car", "pool": "The pool"}

StateReader = Callable[[str], Any]


def _number(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if isfinite(value) else None


def fraction(raw: Any, unit: Any) -> float | None:
    """A state of charge as 0..1, or None when the state is not a number.

    A ``%`` unit is authoritative, so a car at 1 % is not read as full. Without
    a unit the historical rule applies: 0..1 is a fraction, above 1 a percent.
    The result may lie outside 0..1; deciding that is the caller's job.
    """
    value = _number(raw)
    if value is None:
        return None
    if unit == "%" or value > 1:
        return value / 100
    return value


def _issue(device: str, field: str, entity_id: str | None, value: Any, reason: str) -> dict[str, Any]:
    return {
        "device": device,
        "field": field,
        "entity_id": entity_id,
        "value": value if value is None or isinstance(value, (int, float, str)) else str(value),
        "reason": reason,
        "detected_by": "home_assistant",
    }


def _percent(value: float) -> str:
    return f"{round(value * 1000) / 10:g}%"


class _Readings:
    """Collect one pass of issues from Home Assistant state objects."""

    def __init__(self, state_of: StateReader) -> None:
        self.state_of = state_of
        self.issues: list[dict[str, Any]] = []

    def state(self, device: str, field: str, entity_id: Any, label: str) -> Any | None:
        """The entity's state when it has a reading; None otherwise.

        A missing entity is left to the snapshot builder, which reports it as
        the configuration error it is.
        """
        if not isinstance(entity_id, str) or not entity_id:
            return None
        state = self.state_of(entity_id)
        if state is None:
            return None
        if state.state in UNREADABLE or state.state in (None, ""):
            self.issues.append(_issue(device, field, entity_id, state.state or None,
                                      f"{_NAMES[device]}'s {label} is {state.state or 'empty'}."))
            return None
        return state

    def number(self, device: str, field: str, entity_id: Any, label: str) -> tuple[Any, float] | None:
        state = self.state(device, field, entity_id, label)
        if state is None:
            return None
        value = _number(state.state)
        if value is None:
            self.issues.append(_issue(device, field, entity_id, state.state,
                                      f"{_NAMES[device]}'s {label} is not a number ({state.state!r})."))
            return None
        return state, value

    def fraction(self, device: str, field: str, entity_id: Any, label: str) -> tuple[Any, float] | None:
        read = self.number(device, field, entity_id, label)
        if read is None:
            return None
        state, _value = read
        value = fraction(state.state, state.attributes.get("unit_of_measurement"))
        if not 0 <= value <= 1:
            self.issues.append(_issue(device, field, entity_id, value,
                                      f"{_NAMES[device]} reported a {label} of {_percent(value)}, outside 0–100%."))
            return None
        return state, value


def _battery(readings: _Readings, options: dict[str, Any], now: datetime) -> None:
    entity = options.get(OPT_BATTERY_SOC_ENTITY)
    read = readings.fraction("battery", "soc", entity, "state of charge")
    if read is not None:
        state, value = read
        reported = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
        if isinstance(reported, datetime) and now - reported > BATTERY_SOC_MAX_AGE:
            minutes = int((now - reported).total_seconds() // 60)
            readings.issues.append(_issue("battery", "soc", entity, value,
                f"The home battery's state of charge has not been reported for {minutes} minutes."))
    for key, (unit, minimum, maximum) in BATTERY_QUANTITIES.items():
        source = options.get(key)
        if not isinstance(source, str) or not source.strip().startswith("sensor."):
            continue  # A typed number is configuration, not a reading.
        entity_id = source.strip()
        label = key.removeprefix("battery_").replace("_", " ")
        quantity = readings.number("battery", key, entity_id, label)
        if quantity is None:
            continue
        state, value = quantity
        factor = QUANTITY_UNITS[unit].get(state.attributes.get("unit_of_measurement"))
        if factor is None:
            continue  # A wrong unit is a setup error the builder reports.
        value *= factor
        if (minimum is not None and value < minimum) or (maximum is not None and value > maximum) or (
                key == "battery_min_soc" and value >= 1):
            shown = _percent(value) if unit == "%" else f"{value:g} {unit}"
            readings.issues.append(_issue("battery", key, entity_id, value,
                                          f"The home battery reported a {label} of {shown}, which cannot be right."))


def _vehicle(readings: _Readings, options: dict[str, Any], devices: list[dict[str, Any]],
             known_capacity_kwh: float | None) -> None:
    mappings = options.get("device_control_mappings") or {}
    pool_entity = options.get(OPT_POOL_WATER_TEMPERATURE_ENTITY)
    for control in sorted({
        str((mappings.get(device["key"]) or {}).get("control_entity_id") or "")
        for device in devices
        if device.get("planning_role") == "controllable"
        and mapped_planning_path(device, mappings.get(device["key"]) or {}, pool_entity) == "ev"
    } - {""}):
        readings.state("ev", "charge_current", control, "charging current control")
    connected = readings.state("ev", "connected", options.get(OPT_EV_CONNECTED_ENTITY), "cable connection")
    if connected is not None and connected.state not in ("on", "off"):
        readings.issues.append(_issue("ev", "connected", options.get(OPT_EV_CONNECTED_ENTITY), connected.state,
                                      f"The car's cable connection reads {connected.state!r}, not on or off."))
    soc = readings.fraction("ev", "soc", options.get(OPT_EV_SOC_ENTITY), "state of charge")
    readings.fraction("ev", "departure_target_soc", options.get(OPT_EV_TARGET_SOC_ENTITY), "charge limit")
    entity = options.get(OPT_EV_ENERGY_REMAINING_ENTITY)
    remaining = readings.number("ev", "energy_remaining", entity, "remaining energy")
    if remaining is None:
        return
    if remaining[1] < 0:
        readings.issues.append(_issue("ev", "energy_remaining", entity, remaining[1],
                                      f"The car reported {remaining[1]:g} kWh of remaining energy, below zero."))
    elif soc is not None and (soc[1] <= 0 or remaining[1] <= 0) and not known_capacity_kwh:
        # An empty car is realistic; its battery size is derived from a
        # reading with charge in it, and none has been seen yet.
        readings.issues.append(_issue("ev", "energy_remaining", entity, remaining[1],
            "The car's battery size cannot be derived while it reports no charge, "
            "and no earlier reading is known. It is planned again once it reports charge."))


def device_measurement_issues(
    options: dict[str, Any],
    state_of: StateReader,
    now: datetime,
    *,
    battery: bool,
    pool: bool,
    ev: bool,
    devices: list[dict[str, Any]] = (),
    known_ev_capacity_kwh: float | None = None,
) -> list[dict[str, Any]]:
    """Every unusable reading of the devices this snapshot will read.

    ``battery``, ``pool`` and ``ev`` say whether the builder reads that device
    at all; a device switched off or not included is never inspected.
    """
    readings = _Readings(state_of)
    if battery:
        _battery(readings, options, now)
    if pool:
        read = readings.number("pool", "water_temperature_c", options.get(OPT_POOL_WATER_TEMPERATURE_ENTITY),
                               "water temperature")
        if read is not None and not POOL_WATER_RANGE_C[0] <= read[1] <= POOL_WATER_RANGE_C[1]:
            readings.issues.append(_issue("pool", "water_temperature_c", options.get(OPT_POOL_WATER_TEMPERATURE_ENTITY),
                read[1], f"The pool reported a water temperature of {read[1]:g} °C, outside −5–60 °C."))
    if ev:
        _vehicle(readings, options, list(devices), known_ev_capacity_kwh)
    return readings.issues
