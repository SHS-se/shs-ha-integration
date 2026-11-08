"""What each variable-power device's quarters are observed with, besides electricity.

A pool device may name its own power setting (a heat pump's output, a pump's
capacity); any other variable-power device is observed at the power or current
control it is mapped to. Any of them may name a meter of the heat it delivers.
Each quarter carries the setting that held through it and the heat metered in
it, so the website can learn draw and efficiency per setting from the same
quarters it already learns electricity from.

Setting values are read from Recorder states; heat from the same five-minute
statistics as device energy. A quarter whose setting changed, or that has no
known value, is sent without one: unknown is not the previous value.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from math import isfinite
from typing import Any

from .device_controls import mapped_planning_path
from .grid_capacity import power_setting_value

QUARTER = timedelta(minutes=15)
SETTING_FIELD = "power_setting_entity_id"
CONTROL_FIELD = "control_entity_id"
HEAT_FIELD = "heat_energy_entity_id"


def _entity(value: Any) -> str | None:
    return value if isinstance(value, str) and "." in value else None


def device_observation_entities(
    devices: list[dict[str, Any]], mappings: dict[str, Any], pool_water_entity: str | None,
) -> dict[str, dict[str, str]]:
    """``{device_key: {"setting": entity, "heat": entity}}`` for every pool or
    variable-power device that has either.

    A pool device is driven as a relay, so its setting is the observation it
    names. Any other variable-power device is driven through its power or
    current control, so that control is its setting unless it names another.
    """
    result: dict[str, dict[str, str]] = {}
    for device in devices:
        mapping = mappings.get(str(device.get("key"))) if isinstance(mappings, dict) else None
        if not isinstance(mapping, dict):
            continue
        pool = mapped_planning_path(device, mapping, pool_water_entity) == "pool"
        variable = (device.get("control_type") or mapping.get("control_type")) == "variable_power"
        if not (pool or variable):
            continue
        setting = _entity(mapping.get(SETTING_FIELD)) or (None if pool else _entity(mapping.get(CONTROL_FIELD)))
        entry = {name: entity for name, entity in (("setting", setting), ("heat", _entity(mapping.get(HEAT_FIELD))))
                 if entity}
        if entry:
            result[str(device["key"])] = entry
    return result


def observation_signature(entities: dict[str, dict[str, str]]) -> str:
    """Identity of what is observed; a change re-sends the retained history once."""
    return json.dumps(entities, sort_keys=True, separators=(",", ":"))


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def quarter_settings(
    states: list[tuple[datetime, Any, Any]], start: datetime, end: datetime,
) -> dict[str, float]:
    """The setting that held through each whole quarter in ``[start, end)``.

    ``states`` are Recorder rows with the state at ``start`` included. An
    unavailable or unknown blip is no setting at all, so it neither ends nor
    changes the value around it. A quarter that saw two different values, or
    none, is left out.
    """
    start, end = _utc(start), _utc(end)
    known: list[tuple[datetime, float]] = []
    for at, state, _attributes in sorted(states, key=lambda row: _utc(row[0])):
        text = power_setting_value(state)
        if text is None:
            continue
        value = float(text)
        if known and known[-1][1] == value:
            continue
        known.append((max(start, _utc(at)), value))
    result: dict[str, float] = {}
    if not known:
        return result
    quarter = start
    index = 0
    while quarter + QUARTER <= end:
        finish = quarter + QUARTER
        while index + 1 < len(known) and known[index + 1][0] <= quarter:
            index += 1
        begins, value = known[index]
        changed = index + 1 < len(known) and known[index + 1][0] < finish
        if begins <= quarter and not changed and isfinite(value):
            result[quarter.isoformat()] = value
        quarter = finish
    return result


def with_device_observations(
    device_quarters: list[dict[str, Any]],
    settings: dict[str, dict[str, float]],
    heat_quarters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach setting and heat to the quarters that carry that device's energy.

    The website stores both on the device's own quarter, so neither is sent
    for a quarter whose electricity is incomplete.
    """
    heat = {row["start"]: row["device_energy_kwh"] for row in heat_quarters}
    for row in device_quarters:
        energy = row.get("device_energy_kwh") or {}
        held = {key: by_start[row["start"]] for key, by_start in settings.items()
                if key in energy and row["start"] in by_start}
        metered = {key: kwh for key, kwh in heat.get(row["start"], {}).items() if key in energy}
        if held:
            row["device_power_setting"] = held
        if metered:
            row["device_heat_kwh"] = metered
    return device_quarters
