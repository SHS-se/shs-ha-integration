"""Quarter-hour thermal observations for website-side zone learning.

A 1R1C zone fit needs three quantities: indoor temperature, outdoor
temperature, and heat input. The first two are ``measurement`` sensors, so the
recorder keeps five-minute ``mean`` statistics for them and those survive as
long as ``purge_keep_days``. The third already crosses the boundary as
per-device ``device_energy_kwh`` on the electrical actual slots, which beats
any state-derived estimate because it sees an inverter's modulation.

``actuator_duty`` is collected alongside it as a corroborating signal rather
than a substitute: it separates "ran briefly at full power" from "ran all
quarter at low output" for devices whose energy meter is coarse, and it marks
quarters where the zone was never called at all.

Comfort levels and setpoints are **context, not fit inputs**. The optimiser
needs them as planning constraints and the chart needs them to draw a band,
but the physics does not. They are therefore optional on every row, and a
quarter is never dropped for lacking them. They come from state history only
because ``input_number`` helpers carry no ``state_class`` and so have no
statistics at all.

Actuator state and helper values are step functions sampled at irregular
times, which is why both go through one time-weighted integrator rather than a
naive average of the recorded points: five ``on`` rows in a minute must not
outweigh a single ``off`` row that held for the rest of the quarter.

One caveat this module cannot resolve on its own: where a home selects among
several scheduled levels (comfort, setback, sleep) by writing a mode to a
separate helper, ``comfort_min_c``/``comfort_max_c`` describe the envelope
those levels span, not a deadband around one active target. Resolving the
live target needs the mode entity, which the control mapping does not yet
name.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any

try:  # pragma: no cover - exercised by both import paths
    from .optimisation import quarter_start
except ImportError:  # The test suite imports these helpers as flat modules,
    # without Home Assistant installed, so the package parent does not exist.
    from optimisation import quarter_start  # type: ignore[no-redef]

SLOT = timedelta(minutes=15)
SLOT_SECONDS = int(SLOT.total_seconds())

# A zone whose sensors were unavailable for most of a quarter describes
# nothing. Below this share of known seconds the quarter is dropped rather
# than published as a confident reading built from one surviving sample.
THERMAL_MIN_SAMPLE_COVERAGE = 0.6

# Climate entities report their true demand in ``hvac_action``; ``state`` only
# names the mode the user selected. A thermostat left in ``heat`` all night is
# not a heater that ran all night.
ACTIVE_HVAC_ACTIONS = ("heating", "cooling")
UNUSABLE_STATES = ("unknown", "unavailable", "none", "")


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def numeric_value(state: Any, attributes: dict[str, Any] | None = None) -> float | None:
    """Read a helper or sensor state as a number, rejecting placeholders."""
    if isinstance(state, str) and state.strip().lower() in UNUSABLE_STATES:
        return None
    return _as_float(state)


def actuator_value(state: Any, attributes: dict[str, Any] | None = None) -> float | None:
    """Return 1.0 while an actuator is actually running, 0.0 while it is not.

    ``None`` means the actuator's state was not knowable, which is different
    from a confident zero and must not be integrated as one.
    """
    if not isinstance(state, str):
        return None
    text = state.strip().lower()
    if text in UNUSABLE_STATES:
        return None
    action = (attributes or {}).get("hvac_action")
    if isinstance(action, str):
        # A climate entity that reports its action is authoritative about it.
        return 1.0 if action.strip().lower() in ACTIVE_HVAC_ACTIONS else 0.0
    if text == "on":
        return 1.0
    if text == "off":
        return 0.0
    if text in ("heat", "cool", "heat_cool", "auto", "dry", "fan_only"):
        # A climate entity without ``hvac_action`` can only be read by mode.
        # Everything except ``off`` is treated as calling for energy, which
        # overstates duty; the website records the weaker provenance.
        return 1.0
    return None


def quarter_means(
    rows: Iterable[tuple[datetime, float]],
    *,
    min_coverage: float = THERMAL_MIN_SAMPLE_COVERAGE,
) -> dict[datetime, float]:
    """Average recorder five-minute means onto the quarter-hour grid.

    A quarter is only reported when enough of its three five-minute buckets
    exist. A single surviving sample is a snapshot, not the quarter's mean.
    """
    buckets: dict[datetime, dict[datetime, float]] = defaultdict(dict)
    for timestamp, value in rows:
        number = _as_float(value)
        if number is None:
            continue
        aligned = timestamp.astimezone(timezone.utc)
        buckets[quarter_start(aligned)][aligned] = number

    required = max(1, round(3 * min_coverage))
    result: dict[datetime, float] = {}
    for start, samples in buckets.items():
        expected = {start + timedelta(minutes=offset) for offset in (0, 5, 10)}
        usable = {when: value for when, value in samples.items() if when in expected}
        if len(usable) < required:
            continue
        result[start] = round(sum(usable.values()) / len(usable), 4)
    return result


def time_weighted_quarters(
    changes: Sequence[tuple[datetime, Any, dict[str, Any] | None]],
    start: datetime,
    end: datetime,
    value_of: Callable[[Any, dict[str, Any] | None], float | None],
    *,
    min_coverage: float = THERMAL_MIN_SAMPLE_COVERAGE,
) -> dict[datetime, float]:
    """Integrate a step-function history onto the quarter-hour grid.

    Each recorded state is held until the next one, so the value that governed
    the most seconds of a quarter dominates it. Intervals whose value could not
    be read contribute no weight, and a quarter that never reached
    ``min_coverage`` of known seconds is dropped rather than guessed.
    """
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    if end <= start:
        return {}

    ordered = sorted(
        (
            (moment.astimezone(timezone.utc), state, attributes)
            for moment, state, attributes in changes
        ),
        key=lambda row: row[0],
    )
    weighted: dict[datetime, float] = defaultdict(float)
    known: dict[datetime, float] = defaultdict(float)

    for index, (moment, state, attributes) in enumerate(ordered):
        value = value_of(state, attributes)
        if value is None:
            continue
        interval_start = max(moment, start)
        interval_end = ordered[index + 1][0] if index + 1 < len(ordered) else end
        interval_end = min(interval_end, end)
        if interval_end <= interval_start:
            continue
        cursor = interval_start
        while cursor < interval_end:
            slot = quarter_start(cursor)
            boundary = min(slot + SLOT, interval_end)
            seconds = (boundary - cursor).total_seconds()
            weighted[slot] += value * seconds
            known[slot] += seconds
            cursor = boundary

    minimum = SLOT_SECONDS * min_coverage
    return {
        slot: round(weighted[slot] / seconds, 4)
        for slot, seconds in known.items()
        if seconds >= minimum
    }


def interpolate_hourly_forecast(
    records: Iterable[tuple[datetime, float]],
    starts: Sequence[datetime],
) -> dict[datetime, float]:
    """Resample an hourly weather forecast onto planning quarters.

    Linear interpolation between the bracketing hours; quarters outside the
    forecast's own range are left absent rather than flat-extrapolated, so a
    short provider horizon stays visibly short instead of inventing weather.
    """
    points = sorted(
        (moment.astimezone(timezone.utc), value)
        for moment, raw in records
        if (value := _as_float(raw)) is not None
    )
    if not points:
        return {}

    result: dict[datetime, float] = {}
    index = 0
    for slot in starts:
        aligned = slot.astimezone(timezone.utc)
        if aligned < points[0][0] or aligned > points[-1][0]:
            continue
        while index + 1 < len(points) and points[index + 1][0] < aligned:
            index += 1
        left = points[index]
        right = points[index + 1] if index + 1 < len(points) else left
        if right[0] == left[0]:
            result[slot] = round(left[1], 4)
            continue
        ratio = (aligned - left[0]).total_seconds() / (
            right[0] - left[0]
        ).total_seconds()
        result[slot] = round(left[1] + (right[1] - left[1]) * ratio, 4)
    return result


def build_thermal_slots(
    zones: dict[str, dict[str, dict[datetime, float]]],
    outdoor: dict[datetime, float],
) -> list[dict[str, Any]]:
    """Assemble complete quarter-hour thermal rows for the website.

    A zone appears in a quarter when its room temperature is known and its
    actuator state was readable. Comfort levels and setpoints are attached
    when available but never gate a row: they constrain planning and draw the
    chart's band, while the heat-loss fit itself reads temperature against the
    per-device energy already carried on the electrical slots. Outdoor
    temperature is a home-level field because every zone loses heat to the
    same air.
    """
    rows: dict[datetime, dict[str, dict[str, float]]] = defaultdict(dict)
    for key, series in zones.items():
        temperatures = series.get("room_temperature_c", {})
        duties = series.get("actuator_duty", {})
        for slot, temperature in temperatures.items():
            if slot not in duties:
                continue
            observation: dict[str, float] = {
                "room_temperature_c": temperature,
                "actuator_duty": duties[slot],
            }
            for field in ("comfort_min_c", "comfort_max_c", "setpoint_c"):
                value = series.get(field, {}).get(slot)
                if value is not None:
                    observation[field] = value
            rows[slot][key] = observation

    slots: list[dict[str, Any]] = []
    for slot, observations in sorted(rows.items()):
        if not observations:
            continue
        payload: dict[str, Any] = {
            "start": slot.isoformat(),
            "zone_observations": observations,
            "quality": {
                "temperature_aggregation": "mean_of_recorder_5minute_means",
                "duty_aggregation": "time_weighted_recorder_history",
                "duration_seconds": SLOT_SECONDS,
            },
        }
        if slot in outdoor:
            payload["outdoor_temperature_c"] = outdoor[slot]
        slots.append(payload)
    return slots


def thermal_zone_inputs(
    devices: Sequence[dict[str, Any]],
    mappings: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Name the entities each website-selected setpoint zone observes.

    The website owns which devices are controllable; Home Assistant owns the
    entity ids. Only devices where both sides agree on ``setpoint`` are read,
    so a pending or mismatched mapping never contributes observations.
    """
    inputs: dict[str, dict[str, Any]] = {}
    for device in devices:
        if device.get("control_type") != "setpoint":
            continue
        if device.get("planning_role") != "controllable":
            continue
        mapping = mappings.get(str(device["statistic_id"])) or {}
        if mapping.get("control_type") != "setpoint":
            continue
        temperature = mapping.get("temperature_entity_id")
        actuators = [
            entity
            for entity in mapping.get("actuator_entity_ids") or []
            if isinstance(entity, str) and entity.strip()
        ]
        if not isinstance(temperature, str) or not temperature.strip() or not actuators:
            continue
        inputs[str(device["key"])] = {
            "temperature_entity_id": temperature,
            "actuator_entity_ids": actuators,
            "comfort_low_entity_id": mapping.get("comfort_low_entity_id"),
            "comfort_high_entity_id": mapping.get("comfort_high_entity_id"),
            "setpoint_entity_id": mapping.get("setpoint_entity_id"),
        }
    return inputs
