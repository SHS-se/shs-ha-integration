"""Pure helpers for the compact quarter-hour optimisation exchange.

Raw recorder samples stay in Home Assistant. These helpers aggregate energy
counter changes into canonical 900-second buckets, build robust local baseload
profiles, and parse timestamped forecast attributes without provider-specific
or location-specific assumptions.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from math import ceil, isfinite
from statistics import median
from typing import Any, Iterable
from zoneinfo import ZoneInfo

SLOT_SECONDS = 900
SLOT_HOURS = 0.25
SUPPORTED_OPTIMISATION_MODEL_VERSION = "quarter-hour-heuristic-v3"
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


def daily_service_window(
    horizon: list[datetime],
    day: date,
    timezone_name: str,
    deadline_raw: Any,
    baseline_raw: Any,
    *,
    label: str,
) -> tuple[datetime, datetime, datetime] | None:
    """Return one daily service window guaranteed to fit the slot horizon.

    ``24:00`` is a useful human deadline but is not a Python ``time``. Treat
    it as midnight at the end of the named service day. Other clocks must be
    quarter-hour aligned because the planner cannot execute partial slots.
    """
    if not horizon:
        raise OptimisationInputError("optimisation horizon is empty")
    if any(value.tzinfo is None for value in horizon):
        raise OptimisationInputError("optimisation horizon must be timezone-aware")

    def parse_clock(raw: Any, field: str, *, allow_end_of_day: bool) -> tuple[time, int]:
        value = str(raw).strip()
        if allow_end_of_day and value == "24:00":
            return time.min, 1
        try:
            parsed = time.fromisoformat(value)
        except ValueError as err:
            suffix = " or 24:00" if allow_end_of_day else ""
            raise OptimisationInputError(
                f"{field} must be HH:MM{suffix}"
            ) from err
        if parsed.second or parsed.microsecond:
            raise OptimisationInputError(
                f"{field} must align to a 15-minute boundary"
            )
        parsed = parsed.replace(second=0, microsecond=0, tzinfo=None)
        if parsed.minute % 15:
            raise OptimisationInputError(
                f"{field} must align to a 15-minute boundary"
            )
        return parsed, 0

    zone = ZoneInfo(timezone_name)
    deadline_clock, deadline_day_offset = parse_clock(
        deadline_raw, f"{label} deadline", allow_end_of_day=True
    )
    baseline_clock, _ = parse_clock(
        baseline_raw, f"{label} baseline start", allow_end_of_day=False
    )
    first = horizon[0].astimezone(timezone.utc)
    end = horizon[-1].astimezone(timezone.utc) + timedelta(seconds=SLOT_SECONDS)
    local_start = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    deadline = datetime.combine(
        day + timedelta(days=deadline_day_offset), deadline_clock, zone
    ).astimezone(timezone.utc)
    if deadline <= first or deadline > end:
        return None
    earliest = max(first, local_start)
    if earliest >= deadline:
        return None
    last_start = deadline - timedelta(seconds=SLOT_SECONDS)
    baseline = datetime.combine(day, baseline_clock, zone).astimezone(timezone.utc)
    preferred = min(max(earliest, baseline), last_start)
    return earliest, deadline, preferred


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
    """Request a plan before expiry and every quarter after a failed attempt."""
    if now.tzinfo is None:
        raise OptimisationInputError("current time must be timezone-aware")
    if force or retry_after_error or not plan or plan.get("status") != "ready":
        return True
    try:
        valid_until = datetime.fromisoformat(str(plan["valid_until"]))
    except (KeyError, ValueError) as err:
        raise OptimisationInputError("cached plan valid_until is invalid") from err
    if valid_until.tzinfo is None:
        raise OptimisationInputError("cached plan valid_until must include a timezone")
    # Plans live for 75 minutes. A 30-minute margin normally refreshes them
    # every 45 minutes and leaves two quarter-hour retry opportunities.
    return now.astimezone(timezone.utc) >= (
        valid_until.astimezone(timezone.utc) - timedelta(minutes=30)
    )


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
    if not 0.1 <= minimum <= maximum <= 80:
        raise OptimisationInputError(f"{label} current range is invalid")
    if not 0.1 <= step <= maximum:
        raise OptimisationInputError(f"{label} current step is invalid")
    levels = (maximum - minimum) / step
    if abs(levels - round(levels)) > 1e-6:
        raise OptimisationInputError(
            f"{label} maximum current is not aligned to its current step"
        )
    if not phases.is_integer() or not 1 <= phases <= 3:
        raise OptimisationInputError(f"{label} phase count must be 1, 2 or 3")
    if not 100 <= voltage <= 500:
        raise OptimisationInputError(f"{label} voltage is invalid")
    if not 100 <= maximum * phases * voltage <= 100_000:
        raise OptimisationInputError(f"{label} maximum power is invalid")
    return {
        "type": "discrete_current",
        "min_current_a": round(minimum, 6),
        "max_current_a": round(maximum, 6),
        "current_step_a": round(step, 6),
        "phase_count": int(phases),
        "voltage_v": round(voltage, 6),
    }


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
    if plan.get("schema_version") != 3 or plan.get("slot_minutes") != 15:
        raise OptimisationInputError("optimisation plan schema is unsupported")
    if plan.get("mode") != "live":
        raise OptimisationInputError("optimisation plan mode is unsupported")
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
    service_specs: dict[str, dict[str, Any]] = {}
    for service in services:
        if not isinstance(service, dict):
            raise OptimisationInputError("optimisation plan service is invalid")
        service_id = service.get("id")
        device = service.get("device")
        required_kwh = service.get("required_kwh")
        minimum = service.get("min_run_slots")
        earliest = _timestamp(service.get("earliest_start"))
        deadline = _timestamp(service.get("deadline"))
        control = service.get("control")
        if (
            not isinstance(service_id, str)
            or not service_id
            or service_id in service_specs
            or device not in ("pool", "boiler", "ev")
            or isinstance(required_kwh, bool)
            or not isinstance(required_kwh, (int, float))
            or not isfinite(required_kwh)
            or required_kwh < 0
            or isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or minimum < 1
            or earliest is None
            or deadline is None
            or earliest >= deadline
            or not isinstance(control, dict)
        ):
            raise OptimisationInputError("optimisation plan service is invalid")
        if control.get("type") == "fixed_power":
            power_w = control.get("power_w")
            if (
                isinstance(power_w, bool)
                or not isinstance(power_w, (int, float))
                or not isfinite(power_w)
                or not 100 <= power_w <= 100_000
            ):
                raise OptimisationInputError("optimisation fixed-power service is invalid")
            normalized_control: dict[str, Any] = {
                "type": "fixed_power", "power_w": float(power_w)
            }
        elif control.get("type") == "discrete_current":
            if device != "ev":
                raise OptimisationInputError(
                    "optimisation current control is only valid for EVs"
                )
            normalized_control = discrete_current_control(
                control.get("min_current_a"),
                control.get("max_current_a"),
                control.get("current_step_a"),
                control.get("phase_count"),
                control.get("voltage_v"),
                label="optimisation EV service",
            )
        else:
            raise OptimisationInputError("optimisation service control is unsupported")
        service_specs[service_id] = {
            "device": device,
            "required_kwh": float(required_kwh),
            "min_run_slots": minimum,
            "earliest": earliest,
            "deadline": deadline,
            "control": normalized_control,
        }

    plans = plan.get("plans")
    if not isinstance(plans, dict) or set(plans) != {"baseline", "priority", "cost"}:
        raise OptimisationInputError("optimisation scenarios are incomplete")
    expected_starts: list[datetime] | None = None
    expected_binding_flags: list[bool] | None = None
    expected_service_energy: dict[str, float] | None = None
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
            for field in (
                "pool_w", "boiler_w", "ev_w", "ev_target_current_a",
                "ev_min_current_a", "ev_max_current_a",
            ):
                value = slot.get(field)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not isfinite(value)
                    or value < 0
                ):
                    raise OptimisationInputError(f"{key} slot {field} is invalid")
            if not (
                slot["ev_min_current_a"] <= slot["ev_target_current_a"]
                <= slot["ev_max_current_a"]
            ):
                raise OptimisationInputError(
                    f"{key} slot EV current target is outside its envelope"
                )
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
        service_currents = scenario.get("service_currents_a")
        current_service_ids = {
            service_id for service_id, spec in service_specs.items()
            if spec["control"]["type"] == "discrete_current"
        }
        if (
            not isinstance(service_currents, dict)
            or set(service_currents) != current_service_ids
        ):
            raise OptimisationInputError(
                f"{key} scenario current schedule is invalid"
            )
        expected_power = {
            device: [0.0] * len(slots) for device in ("pool", "boiler", "ev")
        }
        expected_current = [0.0] * len(slots)
        envelope_controls: list[list[dict[str, Any]]] = [
            [] for _slot in slots
        ]
        scenario_energy: dict[str, float] = {}
        for service_id, spec in service_specs.items():
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
            ):
                raise OptimisationInputError(
                    f"{key} scenario {service_id} schedule is invalid"
                )
            control = spec["control"]
            if control["type"] == "fixed_power":
                required_count = (
                    0 if spec["required_kwh"] == 0 else max(
                        spec["min_run_slots"],
                        ceil(
                            spec["required_kwh"]
                            / (control["power_w"] / 1000 * SLOT_HOURS)
                            - 1e-9
                        ),
                    )
                )
                if len(indices) not in (
                    {required_count} if scenario["status"] == "ready"
                    else {0, required_count}
                ):
                    raise OptimisationInputError(
                        f"{key} scenario {service_id} workload is invalid"
                    )
                delivered = len(indices) * control["power_w"] / 1000 * SLOT_HOURS
                for index in indices:
                    expected_power[spec["device"]][index] += control["power_w"]
            else:
                currents = service_currents[service_id]
                if not isinstance(currents, list) or len(currents) != len(indices):
                    raise OptimisationInputError(
                        f"{key} scenario {service_id} currents are invalid"
                    )
                available = sum(
                    spec["earliest"] <= start
                    and start + timedelta(minutes=15) <= spec["deadline"]
                    for start in starts
                )
                per_amp_slot_kwh = (
                    control["phase_count"] * control["voltage_v"]
                    / 1000 * SLOT_HOURS
                )
                min_slot_kwh = control["min_current_a"] * per_amp_slot_kwh
                max_slot_kwh = control["max_current_a"] * per_amp_slot_kwh
                minimum_count = (
                    0 if spec["required_kwh"] == 0 else max(
                        spec["min_run_slots"],
                        ceil(spec["required_kwh"] / max_slot_kwh - 1e-9),
                    )
                )
                spread_count = max(
                    minimum_count,
                    int((spec["required_kwh"] + 1e-9) // min_slot_kwh),
                )
                required_count = min(
                    max(minimum_count, available), spread_count
                )
                if len(indices) not in (
                    {required_count} if scenario["status"] == "ready"
                    else {0, required_count}
                ):
                    raise OptimisationInputError(
                        f"{key} scenario {service_id} workload is invalid"
                    )
                for current_a in currents:
                    if (
                        isinstance(current_a, bool)
                        or not isinstance(current_a, (int, float))
                        or not isfinite(current_a)
                        or not control["min_current_a"] <= current_a
                        <= control["max_current_a"]
                        or abs(
                            (current_a - control["min_current_a"])
                            / control["current_step_a"]
                            - round(
                                (current_a - control["min_current_a"])
                                / control["current_step_a"]
                            )
                        ) > 1e-6
                    ):
                        raise OptimisationInputError(
                            f"{key} scenario {service_id} current step is invalid"
                        )
                delivered = sum(currents) * per_amp_slot_kwh
                if scenario["status"] == "ready":
                    base_kwh = required_count * min_slot_kwh
                    increments = max(
                        0,
                        ceil(
                            (spec["required_kwh"] - base_kwh)
                            / (control["current_step_a"] * per_amp_slot_kwh)
                            - 1e-9
                        ),
                    )
                    expected_amps = (
                        required_count * control["min_current_a"]
                        + increments * control["current_step_a"]
                    )
                    if abs(sum(currents) - expected_amps) > 1e-6:
                        raise OptimisationInputError(
                            f"{key} scenario {service_id} current workload is invalid"
                        )
                power_per_amp = control["phase_count"] * control["voltage_v"]
                for index, current_a in zip(indices, currents):
                    expected_power[spec["device"]][index] += (
                        current_a * power_per_amp
                    )
                    expected_current[index] += current_a
                if indices:
                    for index, start in enumerate(starts):
                        if (
                            spec["earliest"] <= start
                            and start + timedelta(minutes=15) <= spec["deadline"]
                        ):
                            envelope_controls[index].append(control)
            if scenario["status"] == "ready" and delivered + 1e-6 < spec["required_kwh"]:
                raise OptimisationInputError(
                    f"{key} scenario {service_id} under-delivers its requirement"
                )
            scenario_energy[service_id] = delivered
        for index, slot in enumerate(slots):
            for device in ("pool", "boiler", "ev"):
                if abs(float(slot[f"{device}_w"]) - expected_power[device][index]) > 0.01:
                    raise OptimisationInputError(
                        f"{key} scenario {device} power is not its discrete schedule"
                    )
            if abs(float(slot["ev_target_current_a"]) - expected_current[index]) > 1e-6:
                raise OptimisationInputError(
                    f"{key} scenario EV current is not its service schedule"
                )
            for field in ("ev_min_current_a", "ev_max_current_a"):
                value = float(slot[field])
                if value == 0:
                    continue
                if not any(
                    control["min_current_a"] <= value
                    <= control["max_current_a"]
                    and abs(
                        (value - control["min_current_a"])
                        / control["current_step_a"]
                        - round(
                            (value - control["min_current_a"])
                            / control["current_step_a"]
                        )
                    ) <= 1e-6
                    for control in envelope_controls[index]
                ):
                    raise OptimisationInputError(
                        f"{key} slot EV current envelope exceeds charger capability"
                    )
        if expected_service_energy is None:
            expected_service_energy = scenario_energy
        elif any(
            abs(scenario_energy[service_id] - expected_service_energy[service_id]) > 1e-6
            for service_id in service_specs
        ):
            raise OptimisationInputError(
                "optimisation scenarios use different service energy"
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
