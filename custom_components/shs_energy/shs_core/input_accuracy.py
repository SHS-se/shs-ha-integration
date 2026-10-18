"""Health of the inputs the planner depends on.

The live checks are one share each — healthy readings over the readings they
expect — so every score is 0..100 by construction. Prices is the cloud's latest
comparison of the planner's price estimates with the prices later published.
The headline averages the checks that have a score.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Callable, Iterable

from .controller_observations import MAX_SAMPLE_AGE_SECONDS
from .optimisation import ACTUAL_FIELD_BY_CATEGORY, OptimisationInputError, quarter_start, solar_forecast

VERSION = 1
QUARTER = timedelta(minutes=15)
MEASUREMENT_WINDOW = timedelta(hours=24)
MAX_ISSUES = 10
# Polled continuously by the plant: silence means stale, not idle.
LIVE_FIELDS = ("battery_soc_entity", "battery_power_measurement_entity", "grid_power_entity",
               "grid_export_power_entity", "house_consumption_power_entity", "solar_production_power_entity")
# Read for planning but reported on change, so only an unusable state counts.
STATE_FIELDS = ("ev_soc_entity", "ev_connected_entity", "ev_energy_remaining_entity",
                "pool_water_temperature_entity", "outdoor_temperature_entity")


def grade(percentage: int | None) -> str | None:
    if percentage is None:
        return None
    return ("green" if percentage >= 90 else "lime" if percentage >= 80 else "yellow" if percentage >= 70
            else "orange" if percentage >= 60 else "red")


def _check(key: str, healthy: int, expected: int, issues: Iterable[str], **extra: Any) -> dict[str, Any] | None:
    if expected <= 0:
        return None
    percentage = round(100 * healthy / expected)
    return {"key": key, "percentage": percentage, "grade": grade(percentage), "healthy": healthy,
            "expected": expected, "issues": sorted(issues)[:MAX_ISSUES], **extra}


def price_check(report: dict | None) -> dict[str, Any]:
    """The twice-daily report's estimate accuracy; unscored until a published price was compared."""
    prices = (report or {}).get("prices") or {}
    percentage = prices.get("percentage")
    return {"key": "prices", "percentage": percentage, "grade": grade(percentage),
            "quarters": int(prices.get("quarters") or 0),
            "calculated_at": (report or {}).get("calculated_at") if prices else None, "issues": []}


def _usable(state: Any, entity: str) -> bool:
    if state is None or state.state in (None, "", "unknown", "unavailable"):
        return False
    if entity.startswith("binary_sensor."):
        return state.state in ("on", "off")
    try:
        return isfinite(float(state.state))
    except (TypeError, ValueError):
        return False


def _fresh(state: Any, now: datetime) -> bool:
    reported = getattr(state, "last_reported", None)
    if not isinstance(reported, datetime):
        return False
    return 0 <= (now - reported).total_seconds() <= MAX_SAMPLE_AGE_SECONDS


def _forecast_usable(state: Any, entity: str) -> bool:
    """A numeric state is not enough: planning reads the timestamped `watts` series."""
    if not _usable(state, entity):
        return False
    try:
        solar_forecast([{"entity_id": entity, "attributes": dict(state.attributes or {}),
                         "last_updated": state.last_updated}])
    except OptimisationInputError:
        return False
    return True


def solar_forecast_check(options: dict, read: Callable[[str], Any]) -> dict[str, Any] | None:
    """Solar forecast sensors planning can read; homes without solar have nothing to check."""
    entities = set(options.get("pv_forecast_entities") or [])
    if not entities and not options.get("entities_solar_production"):
        return None
    if not entities:
        return _check("solar_forecast", 0, 1, ["no solar forecast configured"])
    failed = {entity for entity in entities if not _forecast_usable(read(entity), entity)}
    return _check("solar_forecast", len(entities) - len(failed), len(entities), failed)


def sensor_check(options: dict, read: Callable[[str], Any], now: datetime) -> dict[str, Any] | None:
    """Usable planning sensors; the plant's live readings must also be fresh."""
    excluded = set(options.get("excluded_device_readings") or [])
    live = {options[field] for field in LIVE_FIELDS if isinstance(options.get(field), str)} - excluded
    others = {entity for key, values in options.items() if key.startswith("entities_") and isinstance(values, list)
              for entity in values if isinstance(entity, str)}
    others |= {options[field] for field in STATE_FIELDS if isinstance(options.get(field), str)}
    others -= excluded | live
    failed = {entity for entity in live if not (_usable(read(entity), entity) and _fresh(read(entity), now))}
    failed |= {entity for entity in others if not _usable(read(entity), entity)}
    total = len(live) + len(others)
    return _check("sensors", total - len(failed), total, failed)


def quarter_completeness(rows: list[dict], entities_by_category: dict[str, list[str]],
                         start: datetime, end: datetime) -> dict[str, bool]:
    """Whether every quarter from start to end has the household balance the planner needs."""
    required = {"total_load_kwh"} | {field for category, field in ACTUAL_FIELD_BY_CATEGORY.items()
                                     if entities_by_category.get(category)}
    complete = {datetime.fromisoformat(row["start"]).astimezone(timezone.utc)
                for row in rows if all(row.get(field) is not None for field in required)}
    result, at = {}, quarter_start(start)
    while at < end:
        result[at.isoformat()] = at in complete
        at += QUARTER
    return result


def measurement_check(quarters: dict[str, bool], now: datetime) -> dict[str, Any] | None:
    """Complete measured quarters among those evaluated in the last 24 hours."""
    since = quarter_start(now) - MEASUREMENT_WINDOW
    recent = {start: ok for start, ok in quarters.items() if datetime.fromisoformat(start) >= since}
    gaps = [start for start, ok in recent.items() if not ok]
    return _check("measurements", len(recent) - len(gaps), len(recent), gaps)


def prune_quarters(quarters: dict[str, bool], now: datetime) -> dict[str, bool]:
    since = quarter_start(now) - MEASUREMENT_WINDOW
    return {start: ok for start, ok in quarters.items() if datetime.fromisoformat(start) >= since}


def input_accuracy(*, options: dict, read: Callable[[str], Any], accuracy_report: dict | None,
                   measured_quarters: dict[str, bool], now: datetime) -> dict[str, Any]:
    checks = [check for check in (
        price_check(accuracy_report), solar_forecast_check(options, read),
        sensor_check(options, read, now), measurement_check(measured_quarters, now)) if check is not None]
    scored = [check["percentage"] for check in checks if check["percentage"] is not None]
    percentage = round(sum(scored) / len(scored)) if scored else None
    return {"version": VERSION, "observed_at": now.astimezone(timezone.utc).isoformat(),
            "percentage": percentage, "grade": grade(percentage), "checks": checks}
