"""Room operating electricity from explicit actuator activity, separate from peaks.

A normalized power mean belongs to ON evidence only when its entire five-minute
bucket was observed heating. Neither current standby watts, a mixed-duty mean,
nor the gateway's reservation maximum identifies that operating point.
"""
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from time import perf_counter
import asyncio
import logging

from .device_commands import actuator_targets
from .device_controls import mapped_planning_path

_LOGGER = logging.getLogger(__name__)
PERIOD = timedelta(minutes=5)


@dataclass(frozen=True)
class OnCondition:
    power: str
    on: tuple[str, ...]
    off: tuple[str, ...]


@dataclass(frozen=True)
class OnPowerEvidence:
    expected_w: float | None
    on_hours: float
    intervals: int
    source: str
    condition: OnCondition | None = None

    def as_inference(self):
        return {"method": "room_observed_on_power_v1", **asdict(self)}


def actuator_on(entity, state, attributes):
    """Actual heating is evidence; a thermostat's heat mode is permission."""
    if not isinstance(state, str) or state in ("unknown", "unavailable"):
        return None
    if entity.startswith("climate."):
        action = (attributes or {}).get("hvac_action")
        if action == "heating":
            return True
        if action in ("idle", "off", "cooling") or state == "off":
            return False
        return None
    if entity.startswith(("switch.", "input_boolean.")):
        return {"on": True, "off": False}.get(state)
    return None


def _spans(entity, rows, end):
    # Historical integration orders recorded samples; it does not order commands.
    changes = []
    distinct = {at: (state, attrs) for at, state, attrs in rows}
    for at, (state, attrs) in sorted(distinct.items()):
        value = actuator_on(entity, state, attrs)
        if not changes or changes[-1][1] != value:
            changes.append((at, value))
    return ([at for at, _ in changes], [(at, changes[i+1][0] if i+1 < len(changes) else end, value)
            for i, (at, value) in enumerate(changes)])


def room_on_power(*, conditions, power_means, actuator_rows, start, end):
    """Compute supported means; missing/mixed/shared observations stay unknown."""
    spans = {entity: _spans(entity, rows, end) for entity, rows in actuator_rows.items()}
    def covers(entity, begin, finish, expected):
        starts, intervals = spans.get(entity, ([], []))
        i = bisect_right(starts, begin)-1
        if i < 0:
            return False
        left, right, value = intervals[i]
        return left <= begin and right >= finish and value is expected
    result = {}
    for key, condition in conditions.items():
        energy_wh, hours, count = 0.0, 0.0, 0
        for at, watts in power_means.get(condition.power, []):
            finish = at + PERIOD
            if at < start or finish > end or type(watts) not in (float, int) or not isfinite(watts) or watts < 0:
                continue
            if not condition.on or not all(covers(e, at, finish, True) for e in condition.on):
                continue
            if not all(covers(e, at, finish, False) for e in condition.off):
                continue
            duration = PERIOD.total_seconds()/3600
            energy_wh += watts*duration
            hours += duration
            count += 1
        result[key] = OnPowerEvidence(energy_wh/hours if hours else None, hours, count,
                                     "recorded_on_intervals" if hours else "no_on_coverage", condition)
    return result


async def capture_room_on_power(history, devices, mappings, start, end, *, pool_water_entity=None):
    """Batch the existing history ports; numerical ownership stays in this module."""
    captured, conditions = {}, {}
    for device in devices:
        key = str(device["key"])
        mapping = mappings.get(key, {})
        if mapped_planning_path(device, mapping, pool_water_entity) != "room":
            continue
        power = mapping.get("power")
        if type(power) in (int, float) and isfinite(power) and power > 0:
            captured[key] = OnPowerEvidence(float(power), 0, 0, "configured")
        elif isinstance(power, str):
            other = [m for k, m in mappings.items() if k != key and m.get("power") == power]
            conditions[key] = OnCondition(power, tuple(actuator_targets(mapping)),
                tuple(sorted({entity for m in other for entity in actuator_targets(m)})))
            # A shared meter without another mapping's observable actuator is ambiguous.
            if any(not actuator_targets(m) for m in other):
                captured[key] = OnPowerEvidence(None, 0, 0, "shared_meter_unknown", conditions.pop(key))
        else:
            captured[key] = OnPowerEvidence(None, 0, 0, "no_power_source")
    if not conditions:
        return captured
    sensors = {c.power for c in conditions.values()}
    actuators = sorted({entity for c in conditions.values() for entity in (*c.on, *c.off)})
    begun = perf_counter()
    statistics, states = await asyncio.gather(
        history.statistics(start, end, sensors, "5minute", {"power": "W"}, {"mean"}),
        history.states(start, end, actuators, with_attributes=True))
    means = {}
    for sensor, rows in statistics.items():
        converted = {}
        for row in rows:
            at = row.get("start")
            if isinstance(at, datetime):
                at = at.astimezone(timezone.utc)
            elif type(at) in (float, int) and isfinite(at):
                at = datetime.fromtimestamp(at, timezone.utc)
            else:
                continue
            converted[at] = row.get("mean")
        means[sensor] = sorted(converted.items())
    captured.update(room_on_power(conditions=conditions, power_means=means, actuator_rows=states, start=start, end=end))
    _LOGGER.info("Room electrical evidence: devices=%s power_rows=%s activity_rows=%s on_intervals=%s elapsed_ms=%.1f",
                 len(conditions), sum(map(len, means.values())), sum(map(len, states.values())),
                 sum(v.intervals for v in captured.values()), (perf_counter()-begun)*1000)
    return captured
