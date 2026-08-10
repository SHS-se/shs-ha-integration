"""Pure helpers for the compact quarter-hour optimisation exchange.

Raw recorder samples stay in Home Assistant. These helpers aggregate energy
counter changes into canonical 900-second buckets, build robust local baseload
profiles, and parse timestamped forecast attributes without provider-specific
or location-specific assumptions.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import ceil, isfinite
from statistics import median
from typing import Any, Iterable
from zoneinfo import ZoneInfo

SLOT_SECONDS = 900
SLOT_HOURS = 0.25
SUPPORTED_OPTIMISATION_MODEL_VERSION = "quarter-hour-heuristic-v1"
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
SHIFTABLE_CATEGORIES = ("pool_heating", "hot_water", "ev_charging")


class OptimisationInputError(ValueError):
    """A required optimisation input is absent or ambiguous."""


def quarter_start(value: datetime) -> datetime:
    """Floor an aware timestamp to a UTC quarter-hour boundary."""
    if value.tzinfo is None:
        raise OptimisationInputError("timestamp must be timezone-aware")
    utc = value.astimezone(timezone.utc)
    epoch = int(utc.timestamp())
    return datetime.fromtimestamp(epoch - epoch % SLOT_SECONDS, timezone.utc)


def parse_number(raw: Any, label: str) -> float:
    """Return one finite number; unknown and unit ambiguity fail fast."""
    try:
        value = float(raw)
    except (TypeError, ValueError) as err:
        raise OptimisationInputError(f"{label} is not numeric") from err
    if not isfinite(value):
        raise OptimisationInputError(f"{label} is not finite")
    return value


def normalized_fraction(raw: Any, label: str) -> float:
    """Accept an explicitly fractional or percentage state and normalize it."""
    value = parse_number(raw, label)
    if 0 <= value <= 1:
        return value
    if 1 < value <= 100:
        return value / 100
    raise OptimisationInputError(f"{label} must be 0..1 or 0..100 percent")


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
        raise OptimisationInputError(
            f"none of {', '.join(attribute_names)} contained timestamped values"
        )
    if not issued:
        raise OptimisationInputError("forecast source has no last_updated timestamp")
    return dict(sorted(result.items())), used, max(issued)


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


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise OptimisationInputError("cannot calculate a quantile without samples")
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def build_base_load_profile(
    actual_slots: list[dict[str, Any]],
    timezone_name: str,
    *,
    minimum_samples: int = 3,
    modelled_categories: tuple[str, ...] = SHIFTABLE_CATEGORIES,
    day_type: str | None = None,
) -> list[dict[str, float | int]]:
    """Return 96 robust baseload buckets for a local weekday or weekend."""
    if day_type not in (None, "weekday", "weekend"):
        raise OptimisationInputError("day_type must be weekday or weekend")
    local_tz = ZoneInfo(timezone_name)
    samples: dict[int, list[float]] = defaultdict(list)
    for row in actual_slots:
        total = row.get("total_load_kwh")
        when = _timestamp(row.get("start"))
        if when is None or not isinstance(total, (int, float)) or not isfinite(float(total)):
            continue
        required_fields = [
            ACTUAL_FIELD_BY_CATEGORY[category]
            for category in modelled_categories
        ]
        if any(field not in row for field in required_fields):
            # Absence is unknown, not a zero-energy device.
            continue
        shiftable = sum(
            float(row[ACTUAL_FIELD_BY_CATEGORY[category]])
            for category in modelled_categories
        )
        base_kwh = max(0.0, float(total) - shiftable)
        local = when.astimezone(local_tz)
        if day_type == "weekday" and local.weekday() >= 5:
            continue
        if day_type == "weekend" and local.weekday() < 5:
            continue
        quarter = local.hour * 4 + local.minute // 15
        samples[quarter].append(base_kwh * 4_000)

    missing = [quarter for quarter in range(96) if len(samples[quarter]) < minimum_samples]
    if missing:
        raise OptimisationInputError(
            f"base-load profile lacks {minimum_samples} samples for {len(missing)} quarters"
        )
    return [
        {
            "median_w": round(median(samples[quarter]), 2),
            "p10_w": round(_quantile(samples[quarter], 0.1), 2),
            "p90_w": round(_quantile(samples[quarter], 0.9), 2),
            "sample_count": len(samples[quarter]),
        }
        for quarter in range(96)
    ]


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


def require_fresh_source(
    issued_at: datetime,
    captured_at: datetime,
    *,
    max_age: timedelta,
    label: str,
) -> None:
    """Reject old or future-dated forecast provenance."""
    issued = issued_at.astimezone(timezone.utc)
    captured = captured_at.astimezone(timezone.utc)
    if issued > captured + timedelta(minutes=5):
        raise OptimisationInputError(f"{label} is future-dated")
    if captured - issued > max_age:
        raise OptimisationInputError(f"{label} is stale")


def validate_plan_contract(
    plan: Any, now: datetime, *, require_recent_issue: bool = True
) -> None:
    """Validate the cached server plan before exposing any local request."""
    if not isinstance(plan, dict):
        raise OptimisationInputError("optimisation response has no plan object")
    if plan.get("schema_version") != 1 or plan.get("slot_minutes") != 15:
        raise OptimisationInputError("optimisation plan schema is unsupported")
    if plan.get("model_version") != SUPPORTED_OPTIMISATION_MODEL_VERSION:
        raise OptimisationInputError("optimisation model version is unsupported")
    if plan.get("status") not in ("ready", "incomplete", "infeasible"):
        raise OptimisationInputError("optimisation plan status is invalid")

    current = now.astimezone(timezone.utc)
    issued = _timestamp(plan.get("issued_at"))
    valid_until = _timestamp(plan.get("valid_until"))
    binding_until = _timestamp(plan.get("binding_until"))
    if issued is None or valid_until is None or binding_until is None:
        raise OptimisationInputError("optimisation plan timestamps are invalid")
    if issued > current + timedelta(minutes=5) or (
        require_recent_issue and current - issued > timedelta(minutes=15)
    ):
        raise OptimisationInputError("optimisation plan was not issued recently")
    if valid_until <= current:
        raise OptimisationInputError("optimisation plan is already expired")
    if valid_until <= issued:
        raise OptimisationInputError("optimisation plan validity is invalid")

    services = plan.get("services")
    if not isinstance(services, list):
        raise OptimisationInputError("optimisation plan services are invalid")
    service_specs: dict[str, tuple[str, float, int]] = {}
    for service in services:
        if not isinstance(service, dict):
            raise OptimisationInputError("optimisation plan service is invalid")
        service_id = service.get("id")
        device = service.get("device")
        required_kwh = service.get("required_kwh")
        power_w = service.get("power_w")
        minimum = service.get("min_run_slots")
        if (
            not isinstance(service_id, str)
            or not service_id
            or service_id in service_specs
            or device not in ("pool", "boiler", "ev")
            or isinstance(required_kwh, bool)
            or not isinstance(required_kwh, (int, float))
            or not isfinite(required_kwh)
            or required_kwh < 0
            or isinstance(power_w, bool)
            or not isinstance(power_w, (int, float))
            or not isfinite(power_w)
            or power_w <= 0
            or isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or minimum < 1
        ):
            raise OptimisationInputError("optimisation plan service is invalid")
        count = (
            0
            if required_kwh == 0
            else max(minimum, ceil(required_kwh / (power_w / 1000 * SLOT_HOURS)))
        )
        service_specs[service_id] = (device, float(power_w), count)

    plans = plan.get("plans")
    if not isinstance(plans, dict) or set(plans) != {"baseline", "priority", "cost"}:
        raise OptimisationInputError("optimisation scenarios are incomplete")
    expected_starts: list[datetime] | None = None
    expected_binding_flags: list[bool] | None = None
    for key in ("baseline", "priority", "cost"):
        scenario = plans[key]
        if not isinstance(scenario, dict) or scenario.get("status") not in (
            "ready", "infeasible"
        ):
            raise OptimisationInputError(f"{key} scenario is invalid")
        slots = scenario.get("slots")
        if not isinstance(slots, list) or not 4 <= len(slots) <= 288:
            raise OptimisationInputError(f"{key} scenario slot count is invalid")
        starts: list[datetime] = []
        binding_flags: list[bool] = []
        previous: datetime | None = None
        for slot in slots:
            if not isinstance(slot, dict):
                raise OptimisationInputError(f"{key} scenario has an invalid slot")
            start = _timestamp(slot.get("start"))
            if start is None or (
                previous is not None
                and start - previous != timedelta(minutes=15)
            ):
                raise OptimisationInputError(f"{key} scenario slots are not contiguous")
            previous = start
            starts.append(start)
            if not isinstance(slot.get("binding"), bool):
                raise OptimisationInputError(f"{key} slot binding flag is invalid")
            binding_flags.append(slot["binding"])
            for field in ("pool_w", "boiler_w", "ev_w"):
                value = slot.get(field)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not isfinite(value)
                    or value < 0
                ):
                    raise OptimisationInputError(f"{key} slot {field} is invalid")
            soc = slot.get("battery_soc")
            if not isinstance(soc, (int, float)) or not isfinite(soc) or not 0 <= soc <= 1:
                raise OptimisationInputError(f"{key} slot battery SOC is invalid")
        if expected_starts is None:
            expected_starts = starts
        elif starts != expected_starts:
            raise OptimisationInputError("optimisation scenarios use different horizons")
        if expected_binding_flags is None:
            expected_binding_flags = binding_flags
        elif binding_flags != expected_binding_flags:
            raise OptimisationInputError("optimisation scenarios use different binding slots")

        service_slots = scenario.get("service_slots")
        if not isinstance(service_slots, dict) or set(service_slots) != set(
            service_specs
        ):
            raise OptimisationInputError(f"{key} scenario service schedule is invalid")
        expected_power = {
            device: [0.0] * len(slots) for device in ("pool", "boiler", "ev")
        }
        for service_id, (device, power_w, required_count) in service_specs.items():
            indices = service_slots[service_id]
            if (
                not isinstance(indices, list)
                or any(
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or not 0 <= index < len(slots)
                    for index in indices
                )
                or any(
                    index > 0 and value != indices[index - 1] + 1
                    for index, value in enumerate(indices)
                )
                or len(indices) != required_count
            ):
                raise OptimisationInputError(
                    f"{key} scenario {service_id} schedule is invalid"
                )
            for index in indices:
                expected_power[device][index] += power_w
        for index, slot in enumerate(slots):
            for device in ("pool", "boiler", "ev"):
                if abs(float(slot[f"{device}_w"]) - expected_power[device][index]) > 0.01:
                    raise OptimisationInputError(
                        f"{key} scenario {device} power is not its discrete schedule"
                    )

    if expected_starts is None or expected_binding_flags is None:
        raise OptimisationInputError("optimisation scenarios are empty")
    advisory_seen = False
    for binding in expected_binding_flags:
        if not binding:
            advisory_seen = True
        elif advisory_seen:
            raise OptimisationInputError("binding slots must be one contiguous prefix")
    binding_count = sum(expected_binding_flags)
    expected_binding_until = (
        expected_starts[0]
        if binding_count == 0
        else expected_starts[binding_count - 1] + timedelta(minutes=15)
    )
    if binding_until != expected_binding_until:
        raise OptimisationInputError("binding_until does not match the binding slots")
    if plan.get("status") == "ready" and plans["priority"].get("status") != "ready":
        raise OptimisationInputError("ready plan has an infeasible priority scenario")


def utc_slots(start: datetime, hours: int) -> list[datetime]:
    """Build contiguous real-time slots; DST changes need no special casing."""
    cursor = quarter_start(start)
    if cursor < start.astimezone(timezone.utc):
        cursor += timedelta(seconds=SLOT_SECONDS)
    return [cursor + timedelta(seconds=SLOT_SECONDS * index) for index in range(hours * 4)]
