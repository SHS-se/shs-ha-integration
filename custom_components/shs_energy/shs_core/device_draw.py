"""Electrical evidence from repeated, independently witnessed heating cycles."""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from statistics import median

from .room_electrical import actuator_on


# Power and actuator reports can arrive in either order in Recorder.
EDGE = timedelta(seconds=45)
BASELINE_LOOKBACK = timedelta(minutes=5)
HISTORY_WINDOW = timedelta(days=7)
REFRESH_INTERVAL = timedelta(hours=6)
QUARTER = timedelta(minutes=15)


@dataclass(frozen=True)
class DrawBinding:
    actuator: str
    meter: str


@dataclass(frozen=True)
class DeviceDraw:
    startup_w: float
    running_w: float
    cycles: int
    on_seconds: float
    quarter_kwh: dict[str, float]


@dataclass(frozen=True)
class HeatingCycle:
    started_at: datetime
    peak_w: float
    watt_seconds: float
    seconds: float
    quarter_kwh: dict[str, float]


def _meter_w(state, attributes):
    unit = (attributes or {}).get('unit_of_measurement')
    if unit not in ('W', 'kW') or isinstance(state, bool):
        return None
    try:
        value = float(state)
    except (ValueError, TypeError):
        return None
    watts = value * (1000 if unit == 'kW' else 1)
    return watts if isfinite(watts) and watts >= 0 else None


def learn_device_draw(binding, activity_rows, meter_rows):
    """Estimate an observed startup envelope and time-weighted ON demand.

    Require complete cycles with a stable local background. Ambiguous cycles
    provide no evidence, and a single isolated power spike cannot size a load.
    """
    activity = []
    for at, state, attrs in sorted(activity_rows, key=lambda row: row[0]):
        active = actuator_on(binding.actuator, state, attrs)
        if not activity or active is not activity[-1][1]:
            activity.append((at, active))
    samples = sorted({at: _meter_w(state, attrs) for at, state, attrs in meter_rows}.items())
    times = [at for at, _ in samples]
    cycles = []
    for index in range(1, len(activity) - 1):
        idle, started, stopped = activity[index - 1:index + 2]
        if (idle[1], started[1], stopped[1]) != (False, True, False):
            continue
        next_change = activity[index + 2][0] if index + 2 < len(activity) else None
        cycle = _heating_cycle(samples, times, idle[0], started[0], stopped[0], next_change)
        if cycle is not None:
            cycles.append(cycle)
    if len(cycles) < 3 or len({cycle.started_at.date() for cycle in cycles}) < 2:
        return None
    peaks = sorted(cycle.peak_w for cycle in cycles)
    if peaks[-1] > 2 * peaks[-2]:
        return None  # An isolated high cycle needs independent corroboration.
    seconds = sum(cycle.seconds for cycle in cycles)
    quarters = {}
    for cycle in cycles:
        for key, kwh in cycle.quarter_kwh.items():
            quarters[key] = quarters.get(key, 0.0) + kwh
    return DeviceDraw(peaks[-1],
                      sum(cycle.watt_seconds for cycle in cycles) / seconds,
                      len(cycles), seconds, quarters)


def _heating_cycle(samples, times, idle, started, stopped, next_change):
    baseline_end = started - EDGE
    if idle > baseline_end or stopped - started < EDGE:
        return None
    baseline_start = max(idle, started - BASELINE_LOOKBACK)
    left = bisect_left(times, baseline_start)
    right = bisect_right(times, baseline_end)
    # A change-only meter can hold the idle value for the entire idle span.
    if left == right and left and times[left - 1] >= idle - EDGE:
        left -= 1
    before = [watts for _, watts in samples[left:right]]
    if not before or None in before:
        return None
    baseline = median(before)
    # Reject baseline variation as well as electrical noise. This threshold
    # qualifies evidence only. It is not a cap or a validity rule for meters.
    noise = max(2.0, max(abs(watts - baseline) for watts in before))
    tolerance = 4 * noise
    left = bisect_left(times, baseline_end)
    right = bisect_right(times, stopped + EDGE)
    cycle = samples[left:right]
    if any(watts is None for _, watts in cycle):
        return None
    rises = [i for i, (at, watts) in enumerate(cycle)
             if at <= started + EDGE and watts - baseline > tolerance]
    if not rises:
        return None
    begin = rises[0]
    falls = [i for i, (at, watts) in enumerate(cycle) if i > begin
             and at >= stopped - EDGE and abs(watts - baseline) <= tolerance]
    if not falls:
        return None
    finish = falls[0]
    off_at = cycle[finish][0]
    after_end = min(stopped + EDGE, next_change) if next_change is not None else stopped + EDGE
    if after_end - off_at < timedelta(seconds=15):
        return None
    # Ignore subsequent power rises near the next recorded activity change.
    if next_change is not None:
        after_end = max(off_at, min(after_end, next_change - EDGE))
    if any(abs(watts - baseline) > tolerance for at, watts in cycle[finish:]
           if at <= after_end):
        return None
    active = [watts - baseline for _, watts in cycle[begin:finish]]
    if not active or min(active) <= tolerance:
        return None
    peak = max(active)
    peak_index = active.index(peak)
    neighbors = active[max(0, peak_index - 1):peak_index] + active[peak_index + 1:peak_index + 2]
    if neighbors and max(neighbors) < peak / 2:
        return None
    elapsed = (off_at - cycle[begin][0]).total_seconds()
    if elapsed <= 0:
        return None
    watt_seconds = 0.0
    quarters = {}
    for i, watts in enumerate(active, begin):
        cursor, end = cycle[i][0].astimezone(timezone.utc), cycle[i+1][0].astimezone(timezone.utc)
        watt_seconds += watts * (end - cursor).total_seconds()
        while cursor < end:
            start = cursor.replace(minute=cursor.minute // 15 * 15, second=0, microsecond=0)
            finish = min(end, start + QUARTER)
            key = start.isoformat()
            quarters[key] = quarters.get(key, 0.0) + watts * (finish - cursor).total_seconds() / 3_600_000
            cursor = finish
    return HeatingCycle(cycle[begin][0], peak, watt_seconds, elapsed, quarters)
