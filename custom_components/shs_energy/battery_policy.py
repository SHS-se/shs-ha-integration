"""Closed reader and C/F ranking for the offline compiler's exact anchors only."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cached_property
import json
from math import isfinite

if __package__:
    from .runtime_json import read_runtime_json
else:
    from runtime_json import read_runtime_json

MAX_POLICY_BYTES = 128000
NUMERICAL_DEADBAND_SEK = 1e-7
COMPONENTS = ("import_sek", "export_sek", "wear_sek", "starts_sek", "shaping_sek",
              "ramp_sek", "service_sek", "terminal_sek", "billable_sek", "total_sek")
ACTION_KEYS = ("charge_w", "discharge_w", "solar_charge_w", "export_w")


def _object(value, keys):
    if type(value) is not dict or set(value) != set(keys.split()):
        raise ValueError("unknown/missing policy fields")
    return value


def _array(value, minimum=0, maximum=288):
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise ValueError("policy array exceeds supported bounds")
    return value


def _number(value, minimum=-1000000, maximum=1000000):
    if type(value) not in (int, float) or not isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("invalid policy numeric value")
    return value


def _int(value, minimum=0, maximum=40000000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid policy integer")
    return value


def _id(value):
    if type(value) is not str or not 0 < len(value) <= 128:
        raise ValueError("invalid policy identity")
    return value


def _equal(left, right):
    if abs(left - right) > NUMERICAL_DEADBAND_SEK:
        raise ValueError("policy economic components do not reconcile")


def canonical_json(value):
    def normalize(item):
        if type(item) is float and item.is_integer():
            return int(item)
        if type(item) is dict:
            return {k: normalize(v) for k, v in item.items()}
        if type(item) is list:
            return [normalize(v) for v in item]
        return item
    return json.dumps(normalize(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _time(value):
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError("expected an absolute UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        milliseconds = (parsed - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() * 1000
        if milliseconds < 0 or not milliseconds.is_integer():
            raise ValueError("unsupported timestamp precision")
        return int(milliseconds)
    except (ValueError, OverflowError) as error:
        raise ValueError("invalid policy timestamp") from error


def _series(values, n, *, boolean=False, minimum=0):
    for value in _array(values, n, n):
        if boolean:
            if type(value) is not bool:
                raise ValueError("expected explicit boolean permission")
        else:
            _number(value, minimum)


def parse_problem(value):
    """Validate the supported one-battery, no-service subset of the source model."""
    p = _object(value, "schema_version identity intervals plant economics")
    if type(p["schema_version"]) is not int or p["schema_version"] != 1:
        raise ValueError("unsupported source problem version")
    identity = _object(p["identity"], "case_id intent_revision model_revision actuals_watermark provenance")
    for key in ("case_id", "intent_revision", "model_revision"):
        _id(identity[key])
    if identity["provenance"] not in ("synthetic", "resolved"):
        raise ValueError("unsupported problem provenance")
    intervals = _array(p["intervals"], 1)
    previous = _time(identity["actuals_watermark"])
    for interval in intervals:
        _object(interval, "start end")
        start, end = _time(interval["start"]), _time(interval["end"])
        if start != previous or end <= start or start // 900000 != (end - 1) // 900000:
            raise ValueError("intervals must be contiguous within UTC quarters")
        previous = end
    n = len(intervals)
    plant = _object(p["plant"], "grid pv_w residual_loads thermal_stores equipment")
    for v in _object(plant["grid"], "import_limit_w export_limit_w").values():
        _number(v, 0)
    _series(plant["pv_w"], n)
    ids = set()
    for load in _array(plant["residual_loads"], 0, 32):
        _object(load, "id power_w")
        if _id(load["id"]) in ids:
            raise ValueError("duplicate source equipment")
        ids.add(load["id"])
        _series(load["power_w"], n)
    if plant["thermal_stores"] != []:
        raise ValueError("thermal models are outside this policy reader")
    battery = _object(_array(plant["equipment"], 1, 1)[0],
                      "id model_id kind available state_kwh charge_max_w discharge_max_w charge_efficiency discharge_efficiency wear_sek_per_kwh grid_charge_allowed export_allowed")
    if battery["kind"] != "battery" or _id(battery["id"]) in ids:
        raise ValueError("expected one distinct battery")
    _id(battery["model_id"])
    bounds = _object(battery["state_kwh"], "initial min max provenance")
    _id(bounds["provenance"])
    for key in ("initial", "min", "max"):
        _number(bounds[key], 0)
    if not bounds["min"] <= bounds["initial"] <= bounds["max"] or bounds["min"] >= bounds["max"]:
        raise ValueError("invalid battery state bounds")
    for key in ("charge_efficiency", "discharge_efficiency"):
        if not 0 < _number(battery[key], 0, 1):
            raise ValueError("invalid efficiency")
    for key in ("charge_max_w", "discharge_max_w", "wear_sek_per_kwh"):
        _number(battery[key], 0)
    for key in ("available", "grid_charge_allowed", "export_allowed"):
        _series(battery[key], n, boolean=True)
    econ = _object(p["economics"], "tariff import_sek_per_kwh export_sek_per_kwh shaping_sek_per_kwh_per_kw ramp_sek_per_kw initial_import_w services completed_event_ids terminal")
    if econ["tariff"] != "energy_only" or any(econ[key] != [] for key in ("services", "completed_event_ids", "terminal")):
        raise ValueError("services, terminal utility and other tariffs are not supported yet")
    for key in ("import_sek_per_kwh", "export_sek_per_kwh"):
        _series(econ[key], n, minimum=-1000000)
    for key in ("shaping_sek_per_kwh_per_kw", "ramp_sek_per_kw"):
        _number(econ[key], 0)
    if econ["initial_import_w"] is not None:
        _number(econ["initial_import_w"], 0)
    return p


def _objective(value, *, delta=False):
    _object(value, " ".join(COMPONENTS))
    for number in value.values():
        _number(number)
    if not delta and any(value[key] < 0 for key in ("wear_sek", "shaping_sek", "ramp_sek")):
        raise ValueError("negative physical cost component")
    _equal(value["billable_sek"], value["import_sek"] - value["export_sek"])
    _equal(value["total_sek"], value["billable_sek"] + sum(value[k] for k in ("wear_sek", "starts_sek", "shaping_sek", "ramp_sek")) - value["service_sek"] - value["terminal_sek"])
    if any(value[key] != 0 for key in ("starts_sek", "service_sek", "terminal_sek")):
        raise ValueError("unsupported cost component for this battery subset")
    return tuple(value[key] for key in COMPONENTS)


def _current_cost(problem, action, curtailed):
    """One supported imposed-response interval; no future rescore or native model."""
    battery = problem["plant"]["equipment"][0]
    charge, discharge = action["charge_w"], action["discharge_w"]
    solar, battery_export = action["solar_charge_w"], action["export_w"]
    pv = problem["plant"]["pv_w"][0] - curtailed
    load = sum(v["power_w"][0] for v in problem["plant"]["residual_loads"])
    imported, exported = max(0, load + charge - discharge - pv), max(0, pv + discharge - charge - load)
    interval = problem["intervals"][0]
    hours = (_time(interval["end"]) - _time(interval["start"])) / 3600000
    stored = battery["state_kwh"]
    endpoint = stored["initial"] + charge * hours / 1000 * battery["charge_efficiency"] - discharge * hours / 1000 / battery["discharge_efficiency"]
    checks = (
        charge <= battery["charge_max_w"] + 1e-8, discharge <= battery["discharge_max_w"] + 1e-8,
        charge <= 1e-8 or discharge <= 1e-8, battery["available"][0] or charge + discharge <= 1e-8,
        solar <= charge + 1e-8, solar <= pv + 1e-8, pv >= -1e-8,
        battery_export <= discharge + 1e-8,
        battery["grid_charge_allowed"][0] or charge - solar <= 1e-8,
        battery["export_allowed"][0] or battery_export <= 1e-8,
        charge - solar <= imported + 1e-8, battery_export <= exported + 1e-8,
        exported - battery_export <= pv - solar + 1e-8, discharge - battery_export <= load + 1e-8,
        imported <= problem["plant"]["grid"]["import_limit_w"] + 1e-8,
        exported <= problem["plant"]["grid"]["export_limit_w"] + 1e-8,
        stored["min"] - 1e-8 <= endpoint <= stored["max"] + 1e-8,
    )
    if not all(checks):
        raise ValueError("current response violates its physical model or permissions")
    economics = problem["economics"]
    cost = dict.fromkeys(COMPONENTS, 0.0)
    cost["import_sek"] = imported / 1000 * hours * economics["import_sek_per_kwh"][0]
    cost["export_sek"] = exported / 1000 * hours * economics["export_sek_per_kwh"][0]
    cost["wear_sek"] = (charge + discharge) / 1000 * hours * battery["wear_sek_per_kwh"]
    cost["shaping_sek"] = .5 * economics["shaping_sek_per_kwh_per_kw"] * (imported / 1000) ** 2 * hours
    if economics["initial_import_w"] is not None:
        cost["ramp_sek"] = abs(imported - economics["initial_import_w"]) / 1000 * economics["ramp_sek_per_kw"]
    cost["billable_sek"] = cost["import_sek"] - cost["export_sek"]
    cost["total_sek"] = cost["billable_sek"] + cost["wear_sek"] + cost["shaping_sek"] + cost["ramp_sek"]
    return _objective(cost)


@dataclass(frozen=True)
class Alternative:
    id: str
    current_path_json: str
    current: tuple[float, ...]
    future_delta: tuple[float, ...]
    full: tuple[float, ...]


@dataclass(frozen=True)
class PolicySummary:
    problem_json: str
    group_id: str
    anchor_ms: int
    segment_end_ms: int
    reference_id: str
    alternatives: tuple[Alternative, ...]
    expected_measurements: tuple[tuple[str, float], ...]
    external_import_w: float
    external_export_w: float
    grid_import_limit_w: float
    grid_export_limit_w: float


def _summary(source_json):
    if type(source_json) is not str or len(source_json.encode()) > MAX_POLICY_BYTES:
        raise ValueError("policy exceeds supported byte bound")
    value = _object(read_runtime_json(source_json), "status compiler_version scorer_version identity reference_id coverage search_domain alternatives omitted ranking work")
    if (value["status"], value["compiler_version"], value["scorer_version"]) != ("compiled", "offline-battery-policy-v1", "offline-household-v1"):
        raise ValueError("unsupported compiler result")
    coverage = _object(value["coverage"], "kind problem segment_end current_interval_count response_model interpolation optimality approximation_error_bound_sek")
    if (coverage["kind"], coverage["response_model"], coverage["interpolation"], coverage["optimality"]) != ("exact_problem_anchor", "explicit_imposed_power", "unsupported", "optimal_in_declared_graph") or coverage["approximation_error_bound_sek"] is not None:
        raise ValueError("unsupported coverage or search evidence")
    p = parse_problem(coverage["problem"])
    if value["identity"] != p["identity"]:
        raise ValueError("policy identity differs from its problem")
    anchor = _time(p["identity"]["actuals_watermark"])
    end = _time(coverage["segment_end"])
    if _int(coverage["current_interval_count"], 1, 1) != 1 or end != (anchor // 900000 + 1) * 900000 or _time(p["intervals"][0]["end"]) != end:
        raise ValueError("this reader requires one explicit current response interval")
    domain = _object(value["search_domain"], "energy_levels_kwh pv_curtailment_fractions includes_minimum_export_limit_curtailment retained_per_level")
    for key, maximum, limit in (("energy_levels_kwh", 48, 1000000), ("pv_curtailment_fractions", 4, 1)):
        items = _array(domain[key], 1, maximum)
        for item in items:
            _number(item, 0, limit)
        if items != sorted(set(items)):
            raise ValueError("search levels must be unique and ordered")
    if domain["includes_minimum_export_limit_curtailment"] is not True:
        raise ValueError("unsupported search convention")
    _int(domain["retained_per_level"], 1, 8)
    if value["omitted"] != []:
        raise ValueError("omitted alternatives are outside this initial reader scope")
    battery = p["plant"]["equipment"][0]
    alternatives, deltas = [], []
    work_calls = work_intervals = 0
    for alternative in _array(value["alternatives"], 1, 12):
        _object(alternative, "id candidate full current future_delta total_delta_sek search")
        identity = _id(alternative["id"])
        candidate = _object(alternative["candidate"], "id pv_curtail_w actions")
        if candidate["id"] != identity:
            raise ValueError("candidate identity mismatch")
        n = len(p["intervals"])
        _series(candidate["pv_curtail_w"], n)
        if type(candidate["actions"]) is not dict or set(candidate["actions"]) != {battery["id"]}:
            raise ValueError("candidate must name the exact battery identity")
        actions = _array(candidate["actions"][battery["id"]], n, n)
        for action in actions:
            _object(action, "kind " + " ".join(ACTION_KEYS))
            if action["kind"] != "battery":
                raise ValueError("unsupported response action")
            for key in ACTION_KEYS:
                _number(action[key], 0)
        search = _object(alternative["search"], "scorer_calls interval_evaluations dominated_prefixes pruned_prefixes feasible_extensions exhaustive_in_declared_graph")
        if search["exhaustive_in_declared_graph"] is not True or search["pruned_prefixes"] != 0:
            raise ValueError("search pruning is outside this reader scope")
        for key in search:
            if key != "exhaustive_in_declared_graph":
                _int(search[key])
        work_calls += search["scorer_calls"]
        work_intervals += search["interval_evaluations"]
        current = _objective(alternative["current"])
        for computed, declared in zip(_current_cost(p, actions[0], candidate["pv_curtail_w"][0]), current):
            _equal(computed, declared)
        alternatives.append(Alternative(identity, canonical_json({"actions": actions[:1], "pv_curtail_w": candidate["pv_curtail_w"][:1]}),
                                        current, _objective(alternative["future_delta"], delta=True), _objective(alternative["full"])))
        deltas.append(_number(alternative["total_delta_sek"]))
    if len({a.id for a in alternatives}) != len(alternatives):
        raise ValueError("duplicate alternative identity")
    reference = next((a for a in alternatives if a.id == value["reference_id"]), None)
    if reference is None:
        raise ValueError("missing common reference")
    for alternative, delta in zip(alternatives, deltas):
        for current, base, future, full, base_full in zip(alternative.current, reference.current, alternative.future_delta, alternative.full, reference.full):
            _equal(current - base + future, full - base_full)
        _equal(delta, alternative.full[-1] - reference.full[-1])
    for component in reference.future_delta:
        _equal(component, 0)
    expected_ranking = [a.id for a in sorted(alternatives, key=lambda a: (a.full[-1] - reference.full[-1], a.id))]
    if value["ranking"] != expected_ranking:
        raise ValueError("published ranking does not match reconciled costs")
    work = _object(value["work"], "interval_evaluations_upper_bound interval_evaluations scorer_calls")
    for field in work.values():
        _int(field)
    if work["interval_evaluations"] != work_intervals or work["scorer_calls"] != work_calls or work_intervals > work["interval_evaluations_upper_bound"]:
        raise ValueError("inconsistent work evidence")
    pv = p["plant"]["pv_w"][0]
    load = sum(item["power_w"][0] for item in p["plant"]["residual_loads"])
    measurements = (("energy_kwh", battery["state_kwh"]["initial"]), ("pv_w", pv), ("load_w", load),
                    ("import_sek_per_kwh", p["economics"]["import_sek_per_kwh"][0]),
                    ("export_sek_per_kwh", p["economics"]["export_sek_per_kwh"][0]),
                    *( (key, int(battery[key][0])) for key in ("available", "grid_charge_allowed", "export_allowed")))
    if p["economics"]["initial_import_w"] is not None:
        measurements += (("initial_import_w", p["economics"]["initial_import_w"]),)
    return PolicySummary(canonical_json(p), battery["id"], anchor, end, reference.id, tuple(alternatives), measurements,
                         max(0, load - pv), max(0, pv - load), p["plant"]["grid"]["import_limit_w"], p["plant"]["grid"]["export_limit_w"])


@dataclass(frozen=True)
class BatteryPolicy:
    source_json: str

    def __post_init__(self):
        self.summary  # Validate once; the cache is immutable and never serialized.

    @cached_property
    def summary(self) -> PolicySummary:
        return _summary(self.source_json)


def read_battery_policy(data: bytes) -> BatteryPolicy:
    return BatteryPolicy(canonical_json(read_runtime_json(data)))


def rank_policy(policy: BatteryPolicy, current_id=None, deadband_sek=0.0):
    """Recompute C-Cref+F; current operation wins only within the explicit deadband."""
    _number(deadband_sek, 0, 100)
    summary = policy.summary
    reference = next(a for a in summary.alternatives if a.id == summary.reference_id)
    ranked = sorted(((a.current[-1] - reference.current[-1] + a.future_delta[-1], a.id) for a in summary.alternatives))
    current = next((item for item in ranked if item[1] == current_id), None)
    if current is not None and current[0] - ranked[0][0] <= max(deadband_sek, NUMERICAL_DEADBAND_SEK):
        return current[1], tuple(ranked)
    return min(identity for cost, identity in ranked if cost - ranked[0][0] <= NUMERICAL_DEADBAND_SEK), tuple(ranked)
