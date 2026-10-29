"""Revision-bound house-supply accounting, independent of schedules and device IO."""
from dataclasses import dataclass
from math import isfinite

from .device_power import planned_switch_bindings, read_device_power
from .planning_warnings import planning_warning

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
class DemandLoad:
    """A load the planner separated from base load: one device model key."""
    key: str
    name: str
    category: str

    def __post_init__(self):
        if any(type(v) is not str or not v for v in (self.key, self.name, self.category)) or self.key.startswith("$"):
            raise ValueError("demand loads need a device key, name and category")


@dataclass(frozen=True)
class DemandProfile:
    id: str
    include_base: bool
    eligible_keys: tuple[str, ...]

    def __post_init__(self):
        if type(self.id) is not str or not self.id or type(self.include_base) is not bool:
            raise ValueError("demand profile needs an identity and explicit base membership")
        keys = self.eligible_keys
        if type(keys) is not tuple or tuple(sorted(set(keys))) != keys or any(type(k) is not str or not k for k in keys):
            raise ValueError("eligible keys must be unique and sorted")


@dataclass(frozen=True)
class ExecutionDemand:
    """The plan's demand partition (contract v2). HA reads it; it never infers one.

    Base load is house consumption minus every listed load; a profile's eligible
    demand is base (when included) plus its eligible loads.
    """
    loads: tuple[DemandLoad, ...]
    profiles: tuple[DemandProfile, ...]

    def __post_init__(self):
        keys = tuple(load.key for load in self.loads)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("demand loads must be unique and sorted by key")
        ids = [profile.id for profile in self.profiles]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("demand profiles need unique identities")
        if any(not set(profile.eligible_keys) <= set(keys) for profile in self.profiles):
            raise ValueError("a profile names a load the plan does not list")

    @classmethod
    def read(cls, value):
        if type(value) is not dict or set(value) != {"loads", "profiles"}:
            raise ValueError("unknown or missing demand fields")
        if type(value["loads"]) is not list or type(value["profiles"]) is not list:
            raise ValueError("demand loads and profiles must be arrays")
        loads = []
        for row in value["loads"]:
            if type(row) is not dict or set(row) != {"key", "name", "category"}:
                raise ValueError("unknown or missing demand load fields")
            loads.append(DemandLoad(**row))
        profiles = []
        for row in value["profiles"]:
            if type(row) is not dict or set(row) != {"id", "include_base", "eligible_keys"} or type(row["eligible_keys"]) is not list:
                raise ValueError("unknown or missing demand profile fields")
            profiles.append(DemandProfile(row["id"], row["include_base"], tuple(row["eligible_keys"])))
        return cls(tuple(loads), tuple(profiles))

    @property
    def keys(self):
        return tuple(load.key for load in self.loads)

    def scope(self, profile_id):
        """The profile as a supply scope over exactly this plan's listed loads."""
        profile = next((p for p in self.profiles if p.id == profile_id), None)
        if profile is None:
            raise ValueError("interval names an unknown demand profile")
        return SupplyScope.read({"kind": "selected", "include_base": profile.include_base,
                                 "planned_device_keys": list(profile.eligible_keys)})


@dataclass(frozen=True)
class SupplyAccounting:
    house_w: float
    pv_w: float
    eligible_gross_w: float
    attributed_pv_w: float
    house_supply_bound_w: float
    raw_eligible_gross_w: float
    planning_warnings: tuple[dict, ...] = ()


def proportional_supply(house_w, pv_w, eligible_gross_w):
    house, pv = map(_power, (house_w, pv_w))
    if type(eligible_gross_w) not in (int, float) or not isfinite(eligible_gross_w):
        raise ValueError("eligible power must be finite AC watts")
    raw = float(eligible_gross_w)
    eligible = min(house, max(0.0, raw))
    warnings = () if eligible == raw else (planning_warning('meter_disagreement', 'battery_supply',
        'Device power meters disagree with the measured household total.',
        'Execution continues with battery-eligible demand bounded by the house total and raw excluded demand.',
        evidence={'house_w':house, 'raw_eligible_w':raw, 'eligible_w':eligible}),)
    attributed = min(pv, house) * eligible / house if house else 0.0
    return SupplyAccounting(house, pv, eligible, attributed,
                            max(0.0, min(eligible - attributed, house - pv)), raw, warnings)


@dataclass(frozen=True)
class PowerReading:
    watts: float
    source: str
    at_ms: int
    valid_until_ms: int
    boundary: str
    basis: str = "instantaneous_ac"
    reported_at_ms: int | None = None
    switch_states: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        _power(self.watts)
        if (not self.source or not self.boundary or type(self.at_ms) is not int
                or type(self.valid_until_ms) is not int or not 0 <= self.at_ms < self.valid_until_ms):
            raise ValueError("power evidence requires source, boundary and absolute freshness")
        if self.basis not in ("instantaneous_ac", "interval_average_ac", "switch_off", "switched_ac"):
            raise ValueError("unknown power measurement basis")


def required_power_keys(scope, planned_keys):
    """Meters that remain after cancelling included components from the house total."""
    planned, selected = set(planned_keys), set(scope.planned_device_keys)
    if scope.kind == "selected" and not selected <= planned:
        raise ValueError("scope selects a device outside Planned membership")
    return tuple(sorted(planned - selected if scope.include_base else selected))


def measured_supply(scope, house, pv, planned_readings, planned_keys, *, now_ms,
                    membership_revision, expected_membership_revision, excluded_bounds=None):
    """Missing decomposition is unavailable; no forecast, rating or zero substitute.

    A supplied meter is one disjoint physical component. Hosts must establish
    parent/child non-overlap before constructing this map; duplicate source IDs
    are rejected here as well. Interval averages cannot authorize instant power.
    """
    if (type(now_ms) is not int or now_ms < 0
            or len(set(planned_keys)) != len(planned_keys)):
        raise ValueError("invalid measurement timing or duplicate Planned membership")
    if not membership_revision or membership_revision != expected_membership_revision:
        raise ValueError("supply membership revision changed")
    required = required_power_keys(scope, planned_keys)
    if not set(required) <= set(planned_readings):
        raise ValueError("missing Planned power measurements")
    readings = [house, pv, *(planned_readings[key] for key in sorted(required))]
    if any(not isinstance(r, PowerReading) for r in readings):
        raise ValueError("house and PV power measurements are required")
    if any(r.basis == "interval_average_ac" for r in readings):
        raise ValueError("scope requires instantaneous AC measurements")
    if len({r.boundary for r in readings}) != 1:
        raise ValueError("power measurement boundaries differ")
    if len({r.source for r in readings}) != len(readings):
        raise ValueError("overlapping power measurement sources")
    if scope.kind == "none":
        eligible = 0.0
    elif scope.kind == "whole_house":
        eligible = house.watts
    elif scope.include_base:
        # Base + selected = house - unselected. Selected device measurements
        # cancel exactly; their reporting cadence cannot block this scope.
        eligible = house.watts - sum(max(planned_readings[k].watts,
            _power((excluded_bounds or {}).get(planned_readings[k].source, 0))) for k in required)
    else:
        eligible = sum(planned_readings[k].watts for k in required)
    return proportional_supply(house.watts, pv.watts, eligible)


def observe_supply(scope, options, devices, read_entity, *, at_ms, capture_lifetime_ms,
                   boundary, membership_revision, expected_membership_revision, members=None, excluded_bounds=None):
    """Resolve declared live bindings into one inspectable accounting result.

    `devices` is the acknowledged Included inventory. The host supplies its
    local capture lifetime and metering boundary. Sensor names
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
        return PowerReading(watts, entity, at_ms, at_ms + capture_lifetime_ms, boundary, reported_at_ms=report_ms)

    # A v2 plan lists the loads it separated from base (`members`); only a v1
    # plan leaves that membership to HA's own planned-device inventory.
    planned = set(members) if members is not None else {
        d["key"] for d in devices if d.get("planning_role") == "controllable"
        and d["key"] not in options.get("excluded_device_readings", [])}
    required = required_power_keys(scope, planned)
    mappings = options.get("device_control_mappings", {})
    switches = planned_switch_bindings(options, devices)
    readings = {}
    for key in required:
        entity = mappings.get(key, {}).get("power")
        value = read_device_power(entity, switches.get(key, ()), read_entity, now_ms=at_ms, capture_lifetime_ms=capture_lifetime_ms)
        readings[key] = PowerReading(value.watts, entity, value.at_ms, value.valid_until_ms, boundary,
                                     value.basis, value.reported_at_ms, value.switch_states)
    house = reading(options.get("house_consumption_power_entity"))
    pv = reading(options.get("solar_production_power_entity"))
    result = measured_supply(scope, house, pv, readings, tuple(sorted(planned)), now_ms=at_ms,
        membership_revision=membership_revision,
        expected_membership_revision=expected_membership_revision, excluded_bounds=excluded_bounds)
    return result, (house, pv, *tuple(readings[key] for key in sorted(readings)))
