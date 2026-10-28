"""Pure helpers for the compact quarter-hour optimisation exchange.

Raw recorder samples stay in Home Assistant. These helpers aggregate energy
counter changes into canonical 900-second buckets, build robust local baseload
profiles, and parse timestamped forecast attributes without provider-specific
or location-specific assumptions.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import isfinite
from statistics import median
from typing import Any, Callable, Iterable
from uuid import UUID
from zoneinfo import ZoneInfo

from .api_contract import SNAPSHOT_SCHEMA_VERSION, SUPPORTED_PLAN_SCHEMA_VERSIONS
from .measurements import fraction
from .planning_warnings import planning_warning

SLOT_SECONDS = 900
SLOT_HOURS = 0.25

# The snapshot version this build produces, and the plan versions it can read.
#
# Two separate numbers on purpose. The snapshot version tells the server what
# this installation is able to *send* and, through it, what shape of plan it can
# read back — so the server can offer the newer planner only to installations
# that have already updated, and no plan is ever refused during a rollout. That
# negotiation is what the v8 incident lacked: the server changed unilaterally
# and every installation stopped executing.
#
# Schema 6 adds measured pool state to the snapshot, which is what lets the pool
# be planned as a store with a temperature rather than a fixed daily energy
# budget. A build that cannot send pool state cannot be given a plan that
# assumes it, so the server keeps such a home on the schema 5 planner.
ACTUAL_FIELD_BY_CATEGORY = {
    "total_consumption": "total_load_kwh",
    "solar_production": "solar_production_kwh",
    "grid_import": "grid_import_kwh",
    "grid_export": "grid_export_kwh",
    "pool_heating": "pool_heating_kwh",
    "hot_water": "hot_water_kwh",
    "ev_charging": "ev_charging_kwh",
    "battery_charge": "battery_charge_kwh",
    "battery_discharge": "battery_discharge_kwh",
}


# What the reader can actually do about a gap. The panel steers on this, and
# getting it wrong is worse than saying nothing: "every item below is a field
# on this panel" sent someone hunting a settings page for a shortfall of
# recorder history, which no field on any page can reach.
REMEDY_SETTING = "setting"  # a field on the panel, waiting to be filled in
REMEDY_WAITING = "waiting"  # nothing to fill in; history to accrue, or a source to return
REMEDY_DEFECT = "defect"  # neither — worth reporting rather than hunting for


class OptimisationInputError(ValueError):
    """A required optimisation input is absent or ambiguous.

    ``reasons`` carries every independent gap found in one pass. Reporting them
    together is what stops a multi-device setup from being repaired one
    rediscovered failure at a time.

    ``remedy`` says which kind of gap it is, because the three want opposite
    things from the reader: fill a field, wait, or report a bug. It defaults to
    ``REMEDY_SETTING``. ``fix`` identifies precise local field keys or the
    external settings destination; an untagged error must not invent a local
    field for the reader to change.
    """

    def __init__(self, *reasons: str, remedy: str = REMEDY_SETTING, fix: dict | None = None) -> None:
        super().__init__("; ".join(reasons))
        self.reasons: list[str] = list(reasons)
        self.remedy = remedy
        self.fix = fix


def suggested_load_type(name: str, category: str) -> tuple[str, dict[str, str]]:
    """Suggest one of four editable electrical load characteristics."""
    value = name.lower().replace("_", " ")
    inverter_tokens = (
        "heat pump", "värmepump", "aircon", "air conditioning",
        "mini split", "inverter",
    )
    duty_tokens = (
        "boiler", "water heater", "hot water", "varmvatten", "heater",
        "radiator", "floor heating", "fridge", "freezer", "kyl", "frys",
    )
    variable_tokens = (
        "charger", "charging", "tesla", "computer", "oven", "stove",
        "dishwasher", "washing machine", "tumble dryer", "microwave",
    )
    if any(token in value for token in inverter_tokens):
        load_type, rule = "inverter", "inverter_semantics"
    elif category == "hot_water" or any(token in value for token in duty_tokens):
        load_type, rule = "duty_cycle", "thermostat_semantics"
    elif category in ("ev_charging", "cooling") or any(
        token in value for token in variable_tokens
    ):
        load_type, rule = "variable_full_load", "variable_power_semantics"
    else:
        load_type, rule = "fixed_full_load", "default_fixed_power"
    return load_type, {
        "method": "energy_dashboard_semantics_v1",
        "rule": rule,
        "confidence": "medium" if rule != "default_fixed_power" else "low",
    }


def suggested_device_planning(
    category: str, load_type: str
) -> tuple[str, str | None, dict[str, str]]:
    """Initialize every discovered Energy Dashboard device in base load.

    Category and load-shape inference may propose useful context, but neither
    proves that a customer has installed a safe control path. Controllability
    is therefore always an explicit website opt-in followed by a local Home
    Assistant entity mapping.
    """
    role, control, rule = "base_load", None, "user_opt_in_required"
    return role, control, {
        "method": "energy_dashboard_planning_semantics_v1",
        "rule": rule,
        "load_type_evidence": load_type,
        "confidence": "high",
    }


def quarter_start(value: datetime) -> datetime:
    """Floor an aware timestamp to a UTC quarter-hour boundary."""
    if value.tzinfo is None:
        raise OptimisationInputError(
            "timestamp must be timezone-aware", remedy=REMEDY_DEFECT
        )
    utc = value.astimezone(timezone.utc)
    epoch = int(utc.timestamp())
    return datetime.fromtimestamp(epoch - epoch % SLOT_SECONDS, timezone.utc)


def validate_service_windows(
    services: Iterable[dict[str, Any]], horizon: list[datetime]
) -> None:
    """Fail locally with exact values before an invalid snapshot is uploaded."""
    if not horizon:
        raise OptimisationInputError("optimisation horizon is empty")
    first = horizon[0].astimezone(timezone.utc)
    end = horizon[-1].astimezone(timezone.utc) + timedelta(seconds=SLOT_SECONDS)
    for service in services:
        service_id = str(service.get("id") or "unknown")
        try:
            earliest = datetime.fromisoformat(str(service["earliest_start"]))
            deadline = datetime.fromisoformat(str(service["deadline"]))
        except (KeyError, ValueError) as err:
            raise OptimisationInputError(
                f"{service_id} has an invalid service timestamp"
            ) from err
        if earliest.tzinfo is None or deadline.tzinfo is None:
            raise OptimisationInputError(
                f"{service_id} service timestamps must include a timezone"
            )
        earliest = earliest.astimezone(timezone.utc)
        deadline = deadline.astimezone(timezone.utc)
        if not first <= earliest < deadline <= end:
            raise OptimisationInputError(
                f"{service_id} window {earliest.isoformat()}..{deadline.isoformat()} "
                f"is outside horizon {first.isoformat()}..{end.isoformat()}"
            )


def optimisation_plan_due(
    plan: dict[str, Any] | None,
    now: datetime,
    *,
    force: bool = False,
    retry_after_error: bool = False,
) -> bool:
    """Send a fresh snapshot when the local exchange reaches another quarter.

    The caller runs at startup and on a relative 15-minute interval. Comparing
    quarter buckets tolerates the server's generation time without skipping an
    exchange. The server only solves on new prices or a manual request.
    A missed request never invalidates the cached schedule.
    """
    if now.tzinfo is None:
        raise OptimisationInputError(
            "current time must be timezone-aware", remedy=REMEDY_DEFECT
        )
    if force or retry_after_error or not plan or plan.get("status") != "ready":
        return True
    try:
        issued_at = datetime.fromisoformat(str(plan["issued_at"]))
    except (KeyError, ValueError) as err:
        raise OptimisationInputError("cached plan issued_at is invalid") from err
    if issued_at.tzinfo is None:
        raise OptimisationInputError("cached plan issued_at must include a timezone")
    return quarter_start(now) > quarter_start(issued_at)


def parse_number(raw: Any, label: str) -> float:
    """Return one finite number; unknown and unit ambiguity fail fast."""
    try:
        value = float(raw)
    except (TypeError, ValueError) as err:
        raise OptimisationInputError(f"{label} is not numeric") from err
    if not isfinite(value):
        raise OptimisationInputError(f"{label} is not finite")
    return value


def discrete_current_control(
    min_current_a: Any,
    max_current_a: Any,
    current_step_a: Any,
    phase_count: Any,
    voltage_v: Any,
    *,
    label: str = "EV charger",
) -> dict[str, float | int | str]:
    """Build one validated charger capability from a Home Assistant number."""
    if any(isinstance(value, bool) for value in (
        min_current_a, max_current_a, current_step_a, phase_count, voltage_v
    )):
        raise OptimisationInputError(f"{label} current capability is invalid")
    minimum = parse_number(min_current_a, f"{label} minimum current")
    maximum = parse_number(max_current_a, f"{label} maximum current")
    step = parse_number(current_step_a, f"{label} current step")
    phases = parse_number(phase_count, f"{label} phase count")
    voltage = parse_number(voltage_v, f"{label} voltage")
    if not 0 < minimum <= maximum:
        raise OptimisationInputError(f"{label} current range is invalid")
    if not 0 < step <= maximum:
        raise OptimisationInputError(f"{label} current step is invalid")
    levels = (maximum - minimum) / step
    if abs(levels - round(levels)) > 1e-6:
        raise OptimisationInputError(
            f"{label} maximum current is not aligned to its current step"
        )
    if not phases.is_integer() or not 1 <= phases <= 3:
        raise OptimisationInputError(f"{label} phase count must be 1, 2 or 3")
    if voltage <= 0:
        raise OptimisationInputError(f"{label} voltage is invalid")
    return {
        "type": "discrete_current",
        "min_current_a": round(minimum, 6),
        "max_current_a": round(maximum, 6),
        "current_step_a": round(step, 6),
        "phase_count": int(phases),
        "voltage_v": round(voltage, 6),
    }


def normalized_fraction(raw: Any, label: str, unit: Any = None) -> float:
    """Accept an explicitly fractional or percentage state and normalize it.

    A ``%`` unit decides the scale, so a car at 1 % is not read as full.
    """
    value = fraction(parse_number(raw, label), unit)
    if value is None or not 0 <= value <= 1:
        raise OptimisationInputError(f"{label} must be 0..1 or 0..100 percent")
    return value


def state_is_on(raw: Any) -> bool:
    """Interpret only Home Assistant's explicit boolean states."""
    if raw is True or raw == "on":
        return True
    if raw is False or raw == "off":
        return False
    raise OptimisationInputError("boolean entity must be on or off")


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _forecast_records(value: Any, value_keys: tuple[str, ...]) -> Iterable[tuple[datetime, float]]:
    """Walk common timestamped attribute shapes, never positional arrays."""
    if isinstance(value, dict):
        # Direct {timestamp: value} maps, used by meteo_solar's `watts`.
        direct: list[tuple[datetime, float]] = []
        for key, item in value.items():
            when = _timestamp(key)
            if when is None or not isinstance(item, (int, float)) or not isfinite(float(item)):
                direct = []
                break
            direct.append((when, float(item)))
        if direct:
            yield from direct
            return

        time_value = next(
            (
                value.get(key)
                for key in ("start", "start_time", "startsAt", "datetime", "time")
                if value.get(key) is not None
            ),
            None,
        )
        when = _timestamp(time_value)
        if when is not None:
            raw = next((value.get(key) for key in value_keys if value.get(key) is not None), None)
            if isinstance(raw, (int, float)) and isfinite(float(raw)):
                yield when, float(raw)
                return
        for item in value.values():
            yield from _forecast_records(item, value_keys)
    elif isinstance(value, list):
        for item in value:
            yield from _forecast_records(item, value_keys)


def extract_timestamped_forecast(
    entities: list[dict[str, Any]],
    *,
    attribute_names: tuple[str, ...],
    value_keys: tuple[str, ...],
    combine: str = "unique",
) -> tuple[dict[datetime, float], list[str], datetime]:
    """Extract one unambiguous timestamp→value series from entity attributes."""
    if combine not in ("unique", "sum"):
        raise OptimisationInputError("forecast combination must be unique or sum")
    result: dict[datetime, float] = {}
    used: list[str] = []
    issued: list[datetime] = []
    for entity in entities:
        attributes = entity.get("attributes") or {}
        entity_values: dict[datetime, float] = {}
        for name in attribute_names:
            if name not in attributes:
                continue
            for when, value in _forecast_records(attributes[name], value_keys):
                aligned = quarter_start(when)
                existing = entity_values.get(aligned)
                if existing is not None and abs(existing - value) > 1e-6:
                    raise OptimisationInputError(
                        f"conflicting forecast values at {aligned.isoformat()}"
                    )
                entity_values[aligned] = value
        if entity_values:
            for aligned, value in entity_values.items():
                existing = result.get(aligned)
                if combine == "unique" and existing is not None and abs(
                    existing - value
                ) > 1e-6:
                    raise OptimisationInputError(
                        f"conflicting forecast values at {aligned.isoformat()}"
                    )
                result[aligned] = (
                    result.get(aligned, 0.0) + value
                    if combine == "sum"
                    else value
                )
            used.append(str(entity["entity_id"]))
            updated = _timestamp(entity.get("last_updated"))
            if updated is not None:
                issued.append(updated)
    if not result:
        sources = [entity['entity_id'] for entity in entities]
        raise OptimisationInputError(
            f"none of {', '.join(attribute_names)} contained timestamped values in {', '.join(sources)}",
            fix=({'kind':'entity','entity_id':sources[0]} if len(sources)==1
                 else {'kind':'entities','entity_ids':sources}),
        )
    if not issued:
        raise OptimisationInputError("forecast source has no last_updated timestamp")
    return dict(sorted(result.items())), used, max(issued)


def solar_forecast(
    entities: list[dict[str, Any]],
) -> tuple[dict[datetime, float], list[str], datetime]:
    """The summed solar forecast in W, read the one way planning reads it."""
    return extract_timestamped_forecast(
        entities,
        # `watts` is the canonical adapter contract. Accepting a generic
        # positional array would reintroduce kW/kWh ambiguity.
        attribute_names=("watts",),
        value_keys=("watts", "power", "value", "estimate"),
        combine="sum",
    )


def category_meters(
    configured: dict[str, list[str]],
    devices: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """The meters each quarter-hour category is summed from, each load once.

    Two lists name meters for a category: the options' Energy Dashboard meters,
    copied from the dashboard when discovery last ran, and the shared device
    inventory, read live from the dashboard and reviewed on the website. Adding
    the second to the first counts a load twice whenever its two meters differ:
    with the charger's older meter still in the options after the dashboard
    had moved to a newer one, every charge was recorded twice, the second time
    an hour late as the slower meter caught up, and the base load derived
    beneath it fell to zero. Nothing in a meter tells a second meter on one
    load from a meter on another, so a category with shared devices is
    measured by those devices alone, the same meters its device quarters
    carry; the options still measure every category that has no shared device.
    """
    result = {category: list(meters) for category, meters in configured.items()}
    by_category: dict[str, list[str]] = {}
    for device in devices:
        meters = by_category.setdefault(str(device["category"]), [])
        if device["statistic_id"] not in meters:
            meters.append(str(device["statistic_id"]))
    result.update(by_category)
    return result


def aggregate_category_changes(
    changes: dict[str, list[tuple[datetime, float]]],
) -> list[dict[str, Any]]:
    """Sum three complete 5-minute changes into sparse quarter-hour rows."""
    buckets: dict[datetime, dict[str, float]] = defaultdict(dict)
    for category, rows in changes.items():
        field = ACTUAL_FIELD_BY_CATEGORY.get(category)
        if field is None:
            continue
        per_bucket: dict[datetime, dict[datetime, float]] = defaultdict(dict)
        for timestamp, kwh in rows:
            if not isfinite(kwh) or kwh < 0:
                continue
            per_bucket[quarter_start(timestamp)][timestamp.astimezone(timezone.utc)] = kwh
        for start, samples in per_bucket.items():
            expected = {
                start + timedelta(minutes=offset)
                for offset in (0, 5, 10)
            }
            if set(samples) != expected:
                # A recorder lag or gap is unknown, not a low-consumption
                # quarter. The next overlapping push can fill it by upsert.
                continue
            buckets[start][field] = round(sum(samples.values()), 6)
    return [
        {
            "start": start.isoformat(),
            **values,
            "quality": {
                "aggregation": "sum_of_recorder_5minute_changes",
                "duration_seconds": SLOT_SECONDS,
            },
        }
        for start, values in sorted(buckets.items())
    ]


def aggregate_device_changes(
    changes: dict[str, list[tuple[datetime, float]]],
) -> list[dict[str, Any]]:
    """Keep each Energy Dashboard device as a complete quarter-hour series."""
    buckets: dict[datetime, dict[str, float]] = defaultdict(dict)
    for device_key, rows in changes.items():
        per_bucket: dict[datetime, dict[datetime, float]] = defaultdict(dict)
        for timestamp, kwh in rows:
            if not isfinite(kwh) or kwh < 0:
                continue
            per_bucket[quarter_start(timestamp)][timestamp.astimezone(timezone.utc)] = kwh
        for start, samples in per_bucket.items():
            expected = {
                start + timedelta(minutes=offset) for offset in (0, 5, 10)
            }
            if set(samples) != expected:
                continue
            buckets[start][device_key] = round(sum(samples.values()), 6)
    return [
        {
            "start": start.isoformat(),
            "device_energy_kwh": values,
            "quality": {
                "aggregation": "sum_of_recorder_5minute_changes",
                "duration_seconds": SLOT_SECONDS,
            },
        }
        for start, values in sorted(buckets.items())
    ]


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise OptimisationInputError("cannot calculate a quantile without samples")
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _trimmed_mean(values: list[float]) -> float:
    ordered = sorted(values)
    trim = int(len(ordered) * 0.1) if len(ordered) >= 10 else 0
    kept = ordered[trim:len(ordered) - trim] if trim else ordered
    return sum(kept) / len(kept)


def _weighted_trimmed_mean(pairs: list[tuple[float, float]]) -> float:
    """Recency-weighted trimmed mean of ``(value, weight)`` pairs.

    A trimmed mean rather than a median is intentional for devices. Median
    power turns an intermittent thermostat load into zero for most quarters,
    while copying active power would invent exact switch-on times. The mean is
    the probability-weighted expected draw a planning graph should show.
    """
    if not pairs:
        raise OptimisationInputError("cannot average without samples")
    ordered = sorted(pairs)
    total = sum(weight for _value, weight in ordered)
    if total <= 0:
        return sum(value for value, _weight in ordered) / len(ordered)
    # Trim a tenth of the *weight* from each tail once there is enough evidence
    # to spare it, so one sauna evening cannot define a quarter.
    trim = total * 0.1 if len(ordered) >= 10 else 0.0
    kept_value = 0.0
    kept_weight = 0.0
    seen = 0.0
    for value, weight in ordered:
        low = max(seen, trim)
        high = min(seen + weight, total - trim)
        seen += weight
        usable = high - low
        if usable <= 0:
            continue
        kept_value += value * usable
        kept_weight += usable
    if kept_weight <= 0:
        return sum(value * weight for value, weight in ordered) / total
    return kept_value / kept_weight


def _weighted_quantile(pairs: list[tuple[float, float]], fraction: float) -> float:
    """Quantile of ``(value, weight)`` pairs, interpolating between neighbours.

    Recency weighting makes a plain quantile wrong: three-week-old evidence
    should not count as heavily as yesterday's, and dropping it entirely would
    throw away the only samples a thin day type has.
    """
    if not pairs:
        raise OptimisationInputError("cannot calculate a quantile without samples")
    ordered = sorted(pairs)
    total = sum(weight for _value, weight in ordered)
    if total <= 0:
        return _quantile([value for value, _weight in ordered], fraction)
    target = total * fraction
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= target:
            return value
    return ordered[-1][0]


# Half-life of the recency weighting. A fortnight keeps a month of evidence
# usable while letting a changed household routine take over within weeks.
BASE_LOAD_RECENCY_HALFLIFE_DAYS = 14.0
# Shrinkage constants, in samples. A day type is trusted for its own level once
# a handful of its days have been seen; a per-quarter departure from the shared
# shape needs more evidence than that, because it is a much finer claim.
BASE_LOAD_LEVEL_SHRINKAGE_DAYS = 3.0
BASE_LOAD_SHAPE_SHRINKAGE_SAMPLES = 8.0


def _pooled_weekday_series(
    observations: list[tuple[datetime, int, int, float]],
    *,
    reference: datetime,
    centre: Callable[[list[tuple[float, float]]], float],
    minimum_samples: int,
    label: str,
    quiet_threshold_w: float = 25.0,
    current_residual_w: float | None = None,
) -> dict[str, Any]:
    """Pool a quarter-of-day shape, then let each weekday depart from it.

    Shared by base load and by every metered device, because both had the same
    defect: keying on weekday-versus-weekend gave every future weekday a
    byte-identical series, while leaving a ten-day window with only two or
    three weekend days to speak for every future weekend
    (ENERGY_OPTIMISATION_ARCHITECTURE.md §1.6.3, §1.6.4).

    Splitting into seven independent profiles would make the sample problem far
    worse. This pools instead: one shared shape from every day, a scalar level
    per weekday needing only a few whole days to emerge, and a per-quarter
    deviation held near one until the samples justify moving it. Each departure
    is shrunk by ``n / (n + k)``, so thin evidence collapses to the shared shape
    rather than inventing a routine.

    ``centre`` decides what "typical" means, and the two callers differ for a
    real reason: a weighted median suits residual base load, while a device
    needs a trimmed mean, since a median turns an intermittent thermostat into
    zero for most quarters.
    """
    def weight_of(local: datetime) -> float:
        age_days = max(
            0.0,
            (reference - local.astimezone(timezone.utc)).total_seconds() / 86_400,
        )
        return 0.5 ** (age_days / BASE_LOAD_RECENCY_HALFLIFE_DAYS)

    # 1. The shared quarter-of-day shape, learned from every day in the window.
    pooled: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for local, _weekday, quarter, value in observations:
        pooled[quarter].append((value, weight_of(local)))

    missing = [
        quarter for quarter in range(96)
        if len(pooled[quarter]) < minimum_samples
    ]
    observed = {quarter: centre(pairs) for quarter, pairs in pooled.items() if pairs}
    # Thin evidence is still evidence. Only empty positions need estimation;
    # interpolate around the daily clock rather than discarding a whole home.
    shape = []
    for quarter in range(96):
        if quarter in observed:
            shape.append(observed[quarter])
        elif observed:
            left = next(d for d in range(1, 97) if (quarter-d) % 96 in observed)
            right = next(d for d in range(1, 97) if (quarter+d) % 96 in observed)
            shape.append((observed[(quarter-left) % 96]*right + observed[(quarter+right) % 96]*left)/(left+right))
        else:
            shape.append(current_residual_w or 0.0)
    unobserved = [quarter for quarter in range(96) if quarter not in observed]
    warnings = []
    if missing:
        basis = ('nearby measured quarters' if observed else 'the current measured residual'
                 if current_residual_w is not None else 'an unknown base-load estimate')
        warnings.append(planning_warning('profile_estimated', label,
            f'{label}: {len(unobserved)} unobserved and {len(missing)-len(unobserved)} thin quarter-of-day buckets.',
            f'Planning continues using {basis}; individual observed quarters remain usable.',
            severity='error' if len(unobserved) >= 5 else 'warning',
            evidence={'unobserved_quarters': len(unobserved), 'thin_quarters':len(missing)-len(unobserved),
                      'minimum_samples':minimum_samples, 'basis':basis}))

    # A quarter that is reliably near zero carries no usable ratio, so it is
    # excluded from every ratio below rather than dividing by almost nothing.
    informative = {
        quarter for quarter in range(96) if shape[quarter] > quiet_threshold_w
    }

    # 2. One level per calendar day, then one shrunk level per weekday. Levels
    #    are taken over whole days so that a device which simply runs more on
    #    Saturdays is separated from one that runs at a different hour.
    by_date: dict[Any, list[float]] = defaultdict(list)
    date_weight: dict[Any, float] = {}
    date_weekday: dict[Any, int] = {}
    for local, weekday, quarter, value in observations:
        if quarter not in informative:
            continue
        key = local.date()
        by_date[key].append(value / shape[quarter])
        date_weight[key] = max(date_weight.get(key, 0.0), weight_of(local))
        date_weekday[key] = weekday
    day_levels: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for key, ratios in by_date.items():
        day_levels[date_weekday[key]].append((median(ratios), date_weight[key]))

    level: dict[int, float] = {}
    for weekday in range(7):
        entries = day_levels.get(weekday, [])
        if not entries:
            level[weekday] = 1.0
            continue
        raw = _weighted_quantile(entries, 0.5)
        pull = len(entries) / (len(entries) + BASE_LOAD_LEVEL_SHRINKAGE_DAYS)
        level[weekday] = 1.0 + (raw - 1.0) * pull

    # 3. Per-quarter departures from that level.
    cell: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    for local, weekday, quarter, value in observations:
        if quarter not in informative:
            continue
        expected = shape[quarter] * level[weekday]
        if expected <= 0:
            continue
        ratio = (value / expected, weight_of(local))
        cell[(weekday, quarter)].append(ratio)

    deviation: dict[tuple[int, int], float] = {}
    counts: dict[tuple[int, int], int] = {}
    for weekday in range(7):
        for quarter in range(96):
            samples = cell.get((weekday, quarter), [])
            counts[(weekday, quarter)] = len(samples)
            if quarter in informative and samples:
                pull = len(samples) / (
                    len(samples) + BASE_LOAD_SHAPE_SHRINKAGE_SAMPLES
                )
                deviation[(weekday, quarter)] = 1.0 + (
                    _weighted_quantile(samples, 0.5) - 1.0
                ) * pull
            else:
                deviation[(weekday, quarter)] = 1.0

    return {
        "shape": shape,
        "level": level,
        "deviation": deviation,
        "counts": counts,
        "planning_warnings": warnings,
    }


def build_base_load_model(
    actual_slots: list[dict[str, Any]],
    timezone_name: str,
    *,
    device_slots: list[dict[str, Any]] | None = None,
    modelled_device_keys: tuple[str, ...] = (),
    minimum_samples: int = 3,
    now: datetime | None = None,
    current_residual_w: float | None = None,
) -> dict[str, Any]:
    """Forecast residual base load per weekday and quarter.

    Subtract only device energy measured in the same household quarter.
    A device's forecast is not evidence of its past consumption: projecting a
    new meter's recent running pattern backwards can subtract energy that was
    never used and clamp an otherwise healthy residual to zero.

    Keep quarters with missing device readings, leaving the unmetered share
    inside base load. This can overestimate future uncontrollable demand until
    metering matures, but cannot erase measured household demand by guessing.
    ``estimated_sample_count`` counts these incompletely separated quarters.
    Device planning eligibility is independent of this historical accounting.

    Keying purely on weekday-versus-weekend, as this did until 2026-08-16, has
    two defects that compound. Every future weekday receives a byte-identical
    series, so a plan spanning Monday to Wednesday cannot distinguish them; and
    a ten-day window holds only two or three weekend days, so one atypical
    Saturday becomes the standing expectation for every future weekend quarter
    (ENERGY_OPTIMISATION_ARCHITECTURE.md §1.6.3).

    Splitting into seven independent day-of-week profiles would make the second
    defect far worse — one or two samples per quarter. This estimator therefore
    pools instead of splitting. One shared quarter-of-day **shape** is learned
    from every day, and each weekday earns a departure from it in proportion to
    the evidence it actually has:

    - a scalar **level** per weekday, needing only a few whole days to emerge;
    - a per-quarter **deviation**, held near one until the samples justify it.

    With a thin window every weekday collapses to the shared shape, which is no
    worse than the behaviour it replaces. As history accumulates, genuine
    routine — a late Sunday morning, a Friday evening — emerges on its own. It
    is still a statistical proxy for the four real factors in §1.5.4, and
    occupancy remains the missing one.
    """
    local_tz = ZoneInfo(timezone_name)
    device_energy_by_start: dict[datetime, dict[str, Any]] = {}
    for row in device_slots or []:
        when = _timestamp(row.get("start"))
        values = row.get("device_energy_kwh")
        if when is not None and isinstance(values, dict):
            device_energy_by_start[when.astimezone(timezone.utc)] = values

    estimated_sample_count = 0
    observations: list[tuple[datetime, int, int, float]] = []
    latest: datetime | None = None
    for row in actual_slots:
        total = row.get("total_load_kwh")
        when = _timestamp(row.get("start"))
        if when is None or type(total) not in (int, float) or not isfinite(float(total)) or total < 0:
            continue
        device_values = device_energy_by_start.get(when.astimezone(timezone.utc), {})
        local = when.astimezone(local_tz)
        modelled_kwh = 0.0
        estimated = False
        for key in modelled_device_keys:
            value = device_values.get(key)
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and isfinite(float(value)) and 0 <= value <= 25):
                modelled_kwh += float(value)
            else:
                # Missing is unknown, not an invitation to invent a deduction.
                estimated = True
        estimated_sample_count += int(estimated)
        base_kwh = max(0.0, float(total) - modelled_kwh)
        observations.append((
            local,
            local.weekday(),
            local.hour * 4 + local.minute // 15,
            base_kwh * 4_000,
        ))
        if latest is None or when > latest:
            latest = when

    reference = now or latest
    if reference is None:
        reference = datetime.now(timezone.utc)

    fitted = _pooled_weekday_series(
        observations,
        reference=reference,
        centre=lambda pairs: _weighted_quantile(pairs, 0.5),
        minimum_samples=minimum_samples,
        label="base-load profile",
        current_residual_w=current_residual_w,
    )
    shape = fitted["shape"]
    level = fitted["level"]

    by_weekday: dict[int, list[dict[str, float | int]]] = {}
    for weekday in range(7):
        rows: list[dict[str, float | int]] = []
        for quarter in range(96):
            count = fitted["counts"][(weekday, quarter)]
            centre = (
                shape[quarter] * level[weekday]
                * fitted["deviation"][(weekday, quarter)]
            )
            rows.append({
                "median_w": round(max(0.0, centre), 2),
                "sample_count": count,
            })
        by_weekday[weekday] = rows

    return {
        "by_weekday": by_weekday,
        "sample_count": len(observations),
        "estimated_sample_count": estimated_sample_count,
        "day_levels": {
            weekday: round(value, 4) for weekday, value in level.items()
        },
        "method": "pooled_shape_weekday_level_v1",
        "planning_warnings": fitted["planning_warnings"],
    }


def base_load_source(
    model: dict[str, Any], entity_ids: list[str], issued_at: datetime,
) -> dict[str, Any]:
    """Describe measured recorder provenance separately from model estimates.

    Missing device readings affect the residual estimate, not the origin of
    the whole-house readings. Synthetic sources are demo data and are rejected
    by the live ingestion contract.
    """
    return {
        "provider": "home_assistant_recorder",
        "entity_ids": entity_ids,
        "issued_at": issued_at.isoformat(),
        "valid_until": (issued_at + timedelta(hours=2)).isoformat(),
        "quality": "measured",
        "sample_count": model["sample_count"],
        "estimated_sample_count": model["estimated_sample_count"],
    }


def usable_device_quarter_kwh(value):
    """The existing device electrical domain: a 0..100 kW quarter mean."""
    return type(value) in (int, float) and isfinite(value) and 0 <= value * 4000 <= 100_000


def build_device_load_model(
    device_slots: list[dict[str, Any]],
    device_key: str,
    timezone_name: str,
    *,
    minimum_samples: int = 2,
    now: datetime | None = None,
    allow_partial: bool = False,
) -> dict[str, Any]:
    """Learn one device's expected-power profile per weekday and quarter.

    The same correction as `build_base_load_model`, applied to metered devices.
    Until 2026-08-16 every controllable device — hot water above all — was keyed
    on weekday-versus-weekend, so its forecast for two consecutive weekdays was
    byte-identical and any apparent day-to-day variation in the published plan
    came from the scheduler moving a fixed series, not from the forecast.

    The centre statistic stays a trimmed mean rather than becoming a median, for
    the reason it always was one: an intermittent thermostat load has a median
    of zero in most quarters, and planning against zero would be worse than
    planning against a flat average.
    """
    local_tz = ZoneInfo(timezone_name)
    observations: list[tuple[datetime, int, int, float]] = []
    all_power: list[float] = []
    latest: datetime | None = None
    for row in device_slots:
        when = _timestamp(row.get("start"))
        values = row.get("device_energy_kwh")
        if when is None or not isinstance(values, dict) or device_key not in values:
            continue
        energy = values[device_key]
        if not usable_device_quarter_kwh(energy):
            continue
        local = when.astimezone(local_tz)
        power_w = float(energy) * 4_000
        observations.append((
            local,
            local.weekday(),
            local.hour * 4 + local.minute // 15,
            power_w,
        ))
        all_power.append(power_w)
        if latest is None or when > latest:
            latest = when

    positive = sorted(value for value in all_power if value > 25)
    active_power_w: float | None = None
    if positive:
        threshold = max(25.0, positive[-1] * 0.5)
        active_power_w = round(median(
            value for value in positive if value >= threshold
        ), 1)

    counts = [0] * 96
    totals = [0.0] * 96
    for _local, _weekday, quarter, value in observations:
        counts[quarter] += 1
        totals[quarter] += value
    if allow_partial and min(counts) < minimum_samples:
        # An observed quarter uses its own mean. Unobserved times use the
        # device-wide mean; with no observations we subtract nothing from the
        # household baseline. Rated power is capacity, never assumed demand.
        mean_w = sum(all_power) / len(all_power) if all_power else 0.0
        shape = [
            round(totals[q] / counts[q] if counts[q] else mean_w, 2)
            for q in range(96)
        ]
        return {
            "by_weekday": {day: list(shape) for day in range(7)},
            "sample_count": len(observations),
            "active_power_w": active_power_w,
            "day_levels": {day: 1.0 for day in range(7)},
            "method": "partial_history_mean_v1",
        }

    reference = now or latest
    if reference is None:
        raise OptimisationInputError(
            f"{device_key} has no usable samples", remedy=REMEDY_WAITING
        )
    fitted = _pooled_weekday_series(
        observations, reference=reference, centre=_weighted_trimmed_mean,
        minimum_samples=minimum_samples, label=device_key,
    )
    shape = fitted["shape"]
    level = fitted["level"]

    return {
        "by_weekday": {
            weekday: [
                round(
                    max(
                        0.0,
                        shape[quarter] * level[weekday]
                        * fitted["deviation"][(weekday, quarter)],
                    ),
                    2,
                )
                for quarter in range(96)
            ]
            for weekday in range(7)
        },
        "sample_count": len(observations),
        "active_power_w": active_power_w,
        "day_levels": {
            weekday: round(value, 4) for weekday, value in level.items()
        },
        "method": "pooled_shape_weekday_level_v1",
    }


def service_daily_energy(
    daily_changes: dict[str, dict[str, float]],
    statistic_ids: Iterable[str],
) -> dict[str, float]:
    """Daily energy on exactly the meters one service controls.

    Sizing a service from its whole meter category over-states the requirement
    whenever a sibling meter in that category is planned elsewhere — a room
    heater sharing the pool category, say. A day missing any of the meters is
    dropped rather than counted short.
    """
    wanted = list(statistic_ids)
    totals: dict[str, float] = {}
    for day, values in daily_changes.items():
        measured = [values[value] for value in wanted if value in values]
        if measured and len(measured) == len(wanted):
            totals[day] = sum(measured)
    return totals


def boiler_daily_energy_forecast(device_slots, key, timezone_name, reference, profiles):
    """Calibrate daily kWh from complete days, preserving weekday differences.

    Trimming a sparse quarter-hour profile can remove most thermostat bursts.
    Fit the daily level from daily meter totals instead. Incomplete days stay
    in the quarter profile; they are not treated as zero-consumption days.
    """
    local_tz = ZoneInfo(timezone_name)
    days = {}
    for row in device_slots:
        when = _timestamp(row.get("start"))
        value = (row.get("device_energy_kwh") or {}).get(key)
        if when is None or not usable_device_quarter_kwh(value):
            continue
        day = when.astimezone(local_tz).date()
        days.setdefault(day, {})[when] = value
    samples = []
    for day, quarters in days.items():
        start = datetime.combine(day, datetime.min.time(), local_tz)
        end = start + timedelta(days=1)
        hours = (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() / 3600
        if end > reference or len(quarters) != round(hours * 4):
            continue
        weight = 0.5 ** (max(0, (reference - end).total_seconds()) / 86400 / BASE_LOAD_RECENCY_HALFLIFE_DAYS)
        samples.append((day.weekday(), sum(quarters.values()), weight))
    if not samples:
        return {day: sum(profile) / 4000 for day, profile in profiles.items()}
    shared = sum(kwh * weight for _, kwh, weight in samples) / sum(weight for _, _, weight in samples)
    result = {}
    for weekday in range(7):
        own = [(kwh, weight) for day, kwh, weight in samples if day == weekday]
        weight = sum(w for _, w in own)
        result[weekday] = (shared * BASE_LOAD_LEVEL_SHRINKAGE_DAYS + sum(kwh * w for kwh, w in own)) / (BASE_LOAD_LEVEL_SHRINKAGE_DAYS + weight)
    return result


def service_energy_today(
    device_actuals: list[dict[str, Any]],
    device_keys: Iterable[str],
    today: Any,
    local_tz: Any,
) -> float:
    """Energy those same meters have already delivered today."""
    wanted = set(device_keys)
    return sum(
        float(value)
        for row in device_actuals
        if datetime.fromisoformat(row["start"]).astimezone(local_tz).date() == today
        for key, value in (row.get("device_energy_kwh") or {}).items()
        if key in wanted
    )


def daily_requirement(
    daily_changes: dict[str, float],
    label: str,
    *,
    minimum_days: int = 7,
) -> tuple[float, int]:
    """Median positive daily service energy; quiet/disabled days are excluded."""
    values = [value for value in daily_changes.values() if isfinite(value) and value > 0]
    if len(values) < minimum_days:
        raise OptimisationInputError(
            f"{label} needs {minimum_days} measured active days; found {len(values)}"
        )
    return round(median(values), 3), len(values)


def calibration_summary(
    observations: list[dict[str, float | int]],
    lead_days: int = 4,
    minimum_samples: int = 20,
) -> dict[str, list[float] | list[int]]:
    """Compact PV bias correction by lead; sparse evidence stays neutral."""
    factors: list[float] = []
    counts: list[int] = []
    for lead in range(lead_days):
        rows = [row for row in observations if int(row.get("lead_day", -1)) == lead]
        predicted = sum(float(row.get("predicted_kwh", 0)) for row in rows)
        actual = sum(float(row.get("actual_kwh", 0)) for row in rows)
        # One is the provider's unmodified forecast, not a substituted source.
        factor = (
            actual / predicted
            if predicted > 0 and len(rows) >= minimum_samples
            else 1.0
        )
        factors.append(round(max(0.25, min(2.0, factor)), 4))
        counts.append(len(rows))
    return {
        "correction_factor_by_lead_day": factors,
        "sample_count_by_lead_day": counts,
    }


def validate_plan_contract(plan: Any, now: datetime) -> None:
    """Check only the envelope needed to identify and schedule instructions.

    Forecasts, comparisons and service workload calculations belong to the
    planner. Commands are checked by their device executor against current
    permissions and equipment. A bad device must not discard the household.
    """
    if not isinstance(plan, dict):
        raise OptimisationInputError("optimisation response has no plan object", remedy=REMEDY_DEFECT)
    if (type(plan.get("schema_version")) is not int
            or plan["schema_version"] not in SUPPORTED_PLAN_SCHEMA_VERSIONS
            or plan.get("slot_minutes") != 15):
        raise OptimisationInputError("optimisation plan schema is unsupported", remedy=REMEDY_DEFECT)
    if plan.get("mode") != "live":
        raise OptimisationInputError("optimisation plan mode is unsupported", remedy=REMEDY_DEFECT)
    try:
        UUID(str(plan["plan_id"]))
        UUID(str(plan["snapshot_id"]))
    except (KeyError, TypeError, ValueError, AttributeError) as err:
        raise OptimisationInputError("optimisation plan identifiers are invalid") from err
    issued = _timestamp(plan.get("issued_at"))
    valid_until = _timestamp(plan.get("valid_until"))
    binding_until = _timestamp(plan.get("binding_until"))
    if issued is None or valid_until is None or binding_until is None:
        raise OptimisationInputError("optimisation plan timestamps are invalid")
    if valid_until <= now.astimezone(timezone.utc):
        raise OptimisationInputError("optimisation plan is already expired")
    plans = plan.get("plans")
    priority = plans.get("priority") if isinstance(plans, dict) else None
    slots = priority.get("slots") if isinstance(priority, dict) else None
    if not isinstance(slots, list) or not slots:
        raise OptimisationInputError("optimisation plan has no readable priority schedule")
    previous = None
    for slot in slots:
        start = _timestamp(slot.get("start")) if isinstance(slot, dict) else None
        if start is None or (previous is not None and start < previous + timedelta(minutes=15)):
            raise OptimisationInputError("optimisation plan has invalid or overlapping slot times")
        previous = start
    if plan["schema_version"] >= 9:
        # Schema 10 keeps this envelope; its battery contract states the demand.
        scope = plan.get("operating_scope")
        if (not isinstance(scope, dict) or not isinstance(scope.get("modes"), dict)
                or any(not isinstance(owner, str) or mode not in
                       ("monitoring", "planning", "control_verification", "controlling")
                       for owner, mode in scope["modes"].items())):
            raise OptimisationInputError("plan operating modes are invalid")
        execution = plan.get("execution_plan")
        if (not isinstance(execution, dict) or execution.get("schema_version") in (9, 10, 11)
                or "execution_plan" in execution):
            raise OptimisationInputError("plan requires a self-contained execution plan")
        if any(execution.get(key) != plan.get(key) for key in ("plan_id", "snapshot_id")):
            raise OptimisationInputError("execution plan identity differs")
        validate_plan_contract(execution, now)


def _clock_regime(plan: dict[str, Any], now: datetime) -> tuple:
    """Include each branch's expiry when caching the root's verdict."""
    valid_until = _timestamp(plan.get("valid_until"))
    expired = valid_until <= now.astimezone(timezone.utc) if valid_until else None
    execution = plan.get("execution_plan")
    return (expired, _clock_regime(execution, now) if isinstance(execution, dict) else None)


class PlanContractCache:
    """Validate the owner's current plan once per clock regime, not on every read.

    Entities, the controller and the battery owner look up the binding slot many
    times a minute. Re-checking a multi-megabyte contract on every lookup kept
    Home Assistant's event loop busy. Verdicts, failures included, are kept only
    for the current plan object and its execution branch, and are dropped when
    the owner replaces the plan. A cached plan is replaced, never edited in place.
    """

    def __init__(
        self,
        current_plan: Callable[[], Any],
        validate: Callable[..., None] = validate_plan_contract,
    ) -> None:
        self._current_plan = current_plan
        self._validate = validate
        self._plan: Any = None
        self._verdicts: dict[int, tuple[Any, Any, Exception | None]] = {}

    def __call__(
        self, plan: Any, now: datetime
    ) -> None:
        root = self._current_plan()
        if root is not self._plan:
            self._plan, self._verdicts = root, {}
        if not isinstance(root, dict) or not isinstance(plan, dict) or (
            plan is not root and plan is not root.get("execution_plan")
        ):
            self._validate(plan, now)
            return
        regime = _clock_regime(plan, now)
        cached = self._verdicts.get(id(plan))
        if cached is not None and cached[0] is plan and cached[1] == regime:
            if cached[2] is not None:
                raise cached[2].with_traceback(None)
            return
        try:
            self._validate(plan, now)
        except Exception as error:
            self._verdicts[id(plan)] = (plan, regime, error)
            raise
        self._verdicts[id(plan)] = (plan, regime, None)


def utc_slots(start: datetime, hours: int) -> list[datetime]:
    """Include the current quarter; a snapshot taken seconds late must not skip it."""
    cursor = quarter_start(start)
    return [cursor + timedelta(seconds=SLOT_SECONDS * index) for index in range(hours * 4)]
