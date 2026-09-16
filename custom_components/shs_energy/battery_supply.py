"""Revision-bound house-supply accounting, independent of schedules and device IO."""
from dataclasses import dataclass
from math import isfinite

SOLAR_ATTRIBUTION = "proportional-self-consumed-pv-v1"


def _power(value):
    if type(value) not in (int, float) or not isfinite(value) or value < 0:
        raise ValueError("supply accounting requires finite nonnegative AC watts")
    return float(value)


@dataclass(frozen=True)
class SupplyScope:
    kind: str
    include_base: bool = False
    planned_device_keys: tuple[str, ...] = ()

    def __post_init__(self):
        if self.kind not in ("none", "whole_house", "selected") or type(self.include_base) is not bool:
            raise ValueError("invalid battery supply scope")
        keys = self.planned_device_keys
        if (type(keys) is not tuple or any(type(k) is not str or not k or k.startswith("$") for k in keys)
                or tuple(sorted(set(keys))) != keys):
            raise ValueError("scope keys must be unique canonical Planned device keys")
        if self.kind != "selected" and (self.include_base or keys):
            raise ValueError("only selected scope can name components")
        if self.kind == "selected" and not self.include_base and not keys:
            raise ValueError("empty selected scope must be normalized to none")

    @classmethod
    def read(cls, value):
        if type(value) is not dict:
            raise ValueError("battery supply scope is required")
        kind = value.get("kind")
        fields = {"kind", "include_base", "planned_device_keys"} if kind == "selected" else {"kind"}
        if set(value) != fields:
            raise ValueError("unknown or missing battery supply scope fields")
        if kind == "selected":
            if type(value["planned_device_keys"]) is not list:
                raise ValueError("scope keys must be an array")
            keys = tuple(value["planned_device_keys"])
            if value["include_base"] is False and not keys:
                return cls("none")
            return cls(kind, value["include_base"], keys)
        return cls(kind)


@dataclass(frozen=True)
class SupplyAccounting:
    house_w: float
    pv_w: float
    eligible_gross_w: float
    attributed_pv_w: float
    house_supply_bound_w: float


def proportional_supply(house_w, pv_w, eligible_gross_w):
    house, pv, eligible = map(_power, (house_w, pv_w, eligible_gross_w))
    if eligible > house:
        raise ValueError("eligible consumption exceeds the measured house total")
    attributed = min(pv, house) * eligible / house if house else 0.0
    return SupplyAccounting(house, pv, eligible, attributed,
                            max(0.0, min(eligible - attributed, house - pv)))


@dataclass(frozen=True)
class PowerReading:
    watts: float
    source: str
    at_ms: int
    valid_until_ms: int
    boundary: str
    basis: str = "instantaneous_ac"

    def __post_init__(self):
        _power(self.watts)
        if (not self.source or not self.boundary or type(self.at_ms) is not int
                or type(self.valid_until_ms) is not int or not 0 <= self.at_ms < self.valid_until_ms):
            raise ValueError("power evidence requires source, boundary and absolute freshness")
        if self.basis not in ("instantaneous_ac", "interval_average_ac"):
            raise ValueError("unknown power measurement basis")


def measured_supply(scope, house, pv, planned_readings, planned_keys, *, now_ms,
                    max_alignment_ms, membership_revision, expected_membership_revision):
    """Missing decomposition is unavailable; no forecast, rating or zero substitute.

    A supplied meter is one disjoint physical component. Hosts must establish
    parent/child non-overlap before constructing this map; duplicate source IDs
    are rejected here as well. Interval averages cannot authorize instant power.
    """
    if (type(max_alignment_ms) is not int or max_alignment_ms < 0
            or type(now_ms) is not int or now_ms < 0
            or len(set(planned_keys)) != len(planned_keys)):
        raise ValueError("invalid measurement timing or duplicate Planned membership")
    if not membership_revision or membership_revision != expected_membership_revision:
        raise ValueError("supply membership revision changed")
    if scope.kind == "selected" and not set(scope.planned_device_keys) <= set(planned_keys):
        raise ValueError("scope selects a device outside Planned membership")
    required = set(planned_keys) if scope.include_base else set(scope.planned_device_keys)
    if not required <= set(planned_readings):
        raise ValueError("missing Planned power measurements")
    readings = [house, pv, *(planned_readings[key] for key in sorted(required))]
    if any(not isinstance(r, PowerReading) for r in readings):
        raise ValueError("house and PV power measurements are required")
    if any(r.basis != "instantaneous_ac" or not r.at_ms <= now_ms < r.valid_until_ms for r in readings):
        raise ValueError("scope requires fresh instantaneous AC measurements")
    if len({r.boundary for r in readings}) != 1 or max(r.at_ms for r in readings) - min(r.at_ms for r in readings) > max_alignment_ms:
        raise ValueError("unaligned power measurements")
    if len({r.source for r in readings}) != len(readings):
        raise ValueError("overlapping power measurement sources")
    if scope.kind == "none":
        eligible = 0.0
    elif scope.kind == "whole_house":
        eligible = house.watts
    else:
        base = house.watts - sum(planned_readings[k].watts for k in planned_keys) if scope.include_base else 0.0
        if base < 0:
            raise ValueError("Planned meters exceed the house total")
        eligible = base + sum(planned_readings[k].watts for k in scope.planned_device_keys)
    return proportional_supply(house.watts, pv.watts, eligible)


def observe_supply(scope, options, devices, read_entity, *, at_ms, max_age_ms,
                   max_alignment_ms, boundary, membership_revision, expected_membership_revision):
    """Resolve declared live bindings into one inspectable accounting result.

    `devices` is the acknowledged Included inventory. The host supplies its
    commissioned freshness/alignment budget and metering boundary. Sensor names
    and reviewed running-power estimates cannot establish instantaneous power.
    """
    from datetime import datetime

    def reading(entity):
        if not isinstance(entity, str) or not entity.startswith("sensor."):
            raise ValueError("instantaneous power sensor is not configured")
        value = read_entity(entity)
        if not isinstance(value, dict):
            raise ValueError(f"{entity}: measurement unavailable")
        attributes = value.get("attributes", {})
        unit = attributes.get("unit_of_measurement")
        if unit not in ("W", "kW") or attributes.get("state_class") != "measurement":
            raise ValueError(f"{entity}: instantaneous W or kW measurement required")
        try:
            reported = datetime.fromisoformat(value["last_reported"].replace("Z", "+00:00"))
            if reported.tzinfo is None:
                raise ValueError("timestamp lacks timezone")
            report_ms = int(reported.timestamp() * 1000)
            watts = _power(float(value["state"])) * (1000 if unit == "kW" else 1)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"{entity}: invalid physical power reading") from error
        return PowerReading(watts, entity, report_ms, report_ms + max_age_ms, boundary)

    planned = {d["key"]: d for d in devices if d.get("planning_role") == "controllable"
               and d["key"] not in options.get("excluded_device_readings", [])}
    required = set(planned) if scope.include_base else set(scope.planned_device_keys)
    if not required <= set(planned):
        raise ValueError("supply selects a device outside Planned membership")
    mappings = options.get("device_control_mappings", {})
    readings = {key: reading(mappings.get(key, {}).get("power")) for key in required}
    house = reading(options.get("house_consumption_power_entity"))
    pv = reading(options.get("solar_production_power_entity"))
    result = measured_supply(scope, house, pv, readings, tuple(sorted(planned)), now_ms=at_ms,
        max_alignment_ms=max_alignment_ms, membership_revision=membership_revision,
        expected_membership_revision=expected_membership_revision)
    return result, (house, pv, *tuple(readings[key] for key in sorted(readings)))
