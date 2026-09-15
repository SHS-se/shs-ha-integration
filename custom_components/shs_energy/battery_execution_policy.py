"""Bounded battery execution policy: native current response and compiled continuation.

This module has no device I/O or future optimiser. Native response is a declared
software model; a local catalog and commissioned transport must authorise writes.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from functools import cached_property
import json
from math import isfinite
from typing import Optional

if __package__:
    from .battery_supply import SupplyScope, proportional_supply
    from .runtime_json import read_runtime_json
    from .battery_policy import COMPONENTS, canonical_json
else:
    from battery_supply import SupplyScope, proportional_supply
    from runtime_json import read_runtime_json
    from battery_policy import COMPONENTS, canonical_json

MAX_POLICY_BYTES = 128000
NUMERICAL_TOLERANCE_SEK = 1e-7
PHYSICAL_TOLERANCE = 1e-9
RESPONSE_MODEL = "pv-first-v1"
OPERATIONS = ("self_consumption", "solar_charge", "supply_house", "grid_charge", "export", "hold")
COST_FIELDS = ("import_sek", "export_sek", "wear_sek", "shaping_sek", "ramp_sek", "terminal_sek")
Objective = tuple[float, ...]


def _obj(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise ValueError("unknown or missing execution policy fields")
    return value


def _arr(value, minimum=0, maximum=64):
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise ValueError("execution policy array exceeds its bound")
    return value


def _num(value, minimum=-1e12, maximum=1e12):
    if type(value) not in (int, float) or not isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("expected bounded finite execution number")
    return value


def _int(value, minimum=0, maximum=2 ** 53 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("expected bounded execution integer")
    return value


def _id(value):
    if type(value) is not str or not 0 < len(value) <= 128:
        raise ValueError("invalid execution identity")
    return value


def _bool(value):
    if type(value) is not bool:
        raise ValueError("permission must be an explicit boolean")
    return value


def _range(value):
    pair = tuple(_num(v, 0, 1e6) for v in _arr(value, 2, 2))
    if pair[0] > pair[1]:
        raise ValueError("execution domain bounds are reversed")
    return pair


@dataclass(frozen=True)
class ContextIdentity:
    battery_id: str
    intent_revision: str
    plant_revision: str
    scope_revision: str
    external_scenario_revision: str
    tariff_revision: str
    response_model_revision: str
    catalog_revision: str

    def __post_init__(self):
        for field in fields(self):
            _id(getattr(self, field.name))
        if self.response_model_revision != RESPONSE_MODEL:
            raise ValueError("unsupported native response model")


@dataclass(frozen=True)
class PolicyIdentity:
    policy_id: str
    revision: int
    context: ContextIdentity

    def __post_init__(self):
        _id(self.policy_id)
        _int(self.revision)
        if not isinstance(self.context, ContextIdentity):
            raise ValueError("policy requires a context identity")


@dataclass(frozen=True)
class Permissions:
    available: bool
    grid_charge_allowed: bool
    battery_export_allowed: bool
    export_reserve_kwh: float
    export_price_eligible: bool
    minimum_export_price_sek_per_kwh: float
    price_revision: str

    def __post_init__(self):
        for value in (self.available, self.grid_charge_allowed, self.battery_export_allowed, self.export_price_eligible):
            _bool(value)
        _num(self.export_reserve_kwh, 0, 1e6)
        _num(self.minimum_export_price_sek_per_kwh, -1e6, 1e6)
        _id(self.price_revision)


@dataclass(frozen=True)
class ExecutionConditions:
    revision: int
    at_ms: int
    valid_until_ms: int
    energy_kwh: float
    pv_w: float
    residual_load_w: float
    previous_import_w: float
    identity: ContextIdentity
    permissions: Permissions
    eligible_load_w: Optional[float] = None

    def __post_init__(self):
        if self.eligible_load_w is not None:
            _num(self.eligible_load_w, 0, self.residual_load_w)
        for value in (self.revision, self.at_ms, self.valid_until_ms):
            _int(value)
        if self.at_ms >= self.valid_until_ms:
            raise ValueError("conditions need absolute freshness")
        for value in (self.energy_kwh, self.pv_w, self.residual_load_w, self.previous_import_w):
            _num(value, 0, 1e6)
        if not isinstance(self.identity, ContextIdentity) or not isinstance(self.permissions, Permissions):
            raise ValueError("conditions require configured identity and permissions")


@dataclass(frozen=True)
class BatteryOperation:
    id: str
    operation: str
    charge_limit_w: float
    discharge_limit_w: float

    def __post_init__(self):
        _id(self.id)
        if self.operation not in OPERATIONS:
            raise ValueError("unsupported battery operation")
        c, d = self.charge_limit_w, self.discharge_limit_w
        _num(c, 0, 1e6)
        _num(d, 0, 1e6)
        if ((self.operation in ("solar_charge", "grid_charge") and not (c > 0 and d == 0))
                or (self.operation in ("supply_house", "export") and not (d > 0 and c == 0))
                or (self.operation == "hold" and (c != 0 or d != 0))):
            raise ValueError("operation contradicts its ceilings")

    @property
    def key(self):
        return self.operation, self.charge_limit_w, self.discharge_limit_w


@dataclass(frozen=True)
class BatteryPlant:
    cutoff_kwh: float
    capacity_kwh: float
    charge_max_w: float
    discharge_max_w: float
    charge_efficiency: float
    discharge_efficiency: float
    import_limit_w: float
    export_limit_w: float
    wear_basis: str
    wear_sek_per_kwh: float

    def __post_init__(self):
        if self.wear_basis not in ("ac_throughput", "discharged_storage"):
            raise ValueError("unknown battery wear basis")
        for field in fields(self):
            if field.name != "wear_basis":
                _num(getattr(self, field.name), 0, 1e6)
        if not self.cutoff_kwh < self.capacity_kwh:
            raise ValueError("invalid battery physical state bounds")
        if not 0 < self.charge_efficiency <= 1 or not 0 < self.discharge_efficiency <= 1:
            raise ValueError("invalid battery efficiencies")


@dataclass(frozen=True)
class Economics:
    import_sek_per_kwh: float
    export_sek_per_kwh: float
    shaping_sek_per_kwh_per_kw: float
    ramp_sek_per_kw: float

    def __post_init__(self):
        _num(self.import_sek_per_kwh, -1e6, 1e6)
        _num(self.export_sek_per_kwh, -1e6, 1e6)
        _num(self.shaping_sek_per_kwh_per_kw, 0, 1e6)
        _num(self.ramp_sek_per_kw, 0, 1e6)


@dataclass(frozen=True)
class Domain:
    energy_kwh: tuple[float, float]
    pv_w: tuple[float, float]
    residual_load_w: tuple[float, float]


@dataclass(frozen=True)
class CostFunction:
    polynomial: tuple[float, float, float]
    absolute_terms: tuple[tuple[float, float, float, float], ...]

    def evaluate(self, energy, previous_import):
        a, b, c = self.polynomial
        return a + b * energy + c * energy * energy + sum(
            weight * abs(e * energy + p * previous_import + constant)
            for weight, e, p, constant in self.absolute_terms)


@dataclass(frozen=True)
class ContinuationCell:
    id: str
    witness_id: str
    energy_kwh: tuple[float, float]
    previous_import_w: tuple[float, float]
    inequalities: tuple[tuple[float, float, float], ...]
    cost: tuple[CostFunction, ...]

    def evaluate(self, energy, previous_import):
        eps = PHYSICAL_TOLERANCE
        if (not self.energy_kwh[0] - eps <= energy <= self.energy_kwh[1] + eps
                or not self.previous_import_w[0] - eps <= previous_import <= self.previous_import_w[1] + eps):
            return None
        # Round only numerical boundary noise inward; never extrapolate a cell.
        energy = max(self.energy_kwh[0], min(self.energy_kwh[1], energy))
        previous_import = max(self.previous_import_w[0], min(self.previous_import_w[1], previous_import))
        if any(a * energy + b * previous_import > maximum + eps for a, b, maximum in self.inequalities):
            return None
        values = {key: fn.evaluate(energy, previous_import) for key, fn in zip(COST_FIELDS, self.cost)}
        if any(not isfinite(v) for v in values.values()) or any(values[key] < -NUMERICAL_TOLERANCE_SEK for key in ("wear_sek", "shaping_sek", "ramp_sek", "terminal_sek")):
            raise ValueError("continuation has an invalid economic account")
        return objective(values)


@dataclass(frozen=True)
class PolicyQuality:
    family_id: str
    source_hash: str
    search_exhaustive_in_declared_graph: bool
    search_pruned_prefixes: int
    heldout_count: int
    heldout_max_regret_sek: Optional[float]


@dataclass(frozen=True)
class PolicySummary:
    identity: PolicyIdentity
    actuals_origin_ms: int
    from_ms: int
    refresh_after_ms: int
    until_ms: int
    boundary_ms: int
    domain: Domain
    plant: BatteryPlant
    permissions: Permissions
    economics: Economics
    reference_id: str
    operations: tuple[BatteryOperation, ...]
    cells: tuple[ContinuationCell, ...]
    quality: PolicyQuality
    supply_scope: SupplyScope


def _record(cls, value):
    return cls(**_obj(value, " ".join(f.name for f in fields(cls))))


def _summary(source_json):
    if type(source_json) is not str or len(source_json.encode()) > MAX_POLICY_BYTES:
        raise ValueError("execution policy exceeds 128KB")
    wire = _obj(read_runtime_json(source_json), "schema profile view identity actuals_origin_ms validity domain plant permissions economics reference_id operations continuation quality supply_scope solar_attribution")
    if (wire["schema"], wire["profile"], wire["view"]) != ("battery-execution-policy-v2", "finite-continuation-v1", "executable"):
        raise ValueError("unsupported or conditional execution policy")
    if wire["solar_attribution"] != "proportional-self-consumed-pv-v1":
        raise ValueError("unsupported supply solar attribution")
    supply_scope = SupplyScope.read(wire["supply_scope"])
    raw_id = _obj(wire["identity"], "policy_id revision battery_id intent_revision plant_revision scope_revision external_scenario_revision tariff_revision response_model_revision catalog_revision")
    context = ContextIdentity(**{f.name: raw_id[f.name] for f in fields(ContextIdentity)})
    identity = PolicyIdentity(raw_id["policy_id"], raw_id["revision"], context)
    origin = _int(wire["actuals_origin_ms"])
    validity = _obj(wire["validity"], "from_ms refresh_after_ms until_ms boundary_ms")
    start, refresh, until, boundary = (_int(validity[key]) for key in ("from_ms", "refresh_after_ms", "until_ms", "boundary_ms"))
    if not (origin <= start <= refresh < until <= boundary and boundary == (start // 900000 + 1) * 900000):
        raise ValueError("invalid absolute execution window")
    d = _obj(wire["domain"], "energy_kwh pv_w residual_load_w")
    domain = Domain(*(_range(d[key]) for key in ("energy_kwh", "pv_w", "residual_load_w")))
    plant = _record(BatteryPlant, wire["plant"])
    permissions = _record(Permissions, wire["permissions"])
    economics = _record(Economics, wire["economics"])
    if (domain.energy_kwh[0] < plant.cutoff_kwh or domain.energy_kwh[1] > plant.capacity_kwh
            or not plant.cutoff_kwh <= permissions.export_reserve_kwh <= plant.capacity_kwh
            or permissions.price_revision != context.tariff_revision):
        raise ValueError("policy domain or permissions differ from its plant/tariff")
    operations = tuple(_record(BatteryOperation, v) for v in _arr(wire["operations"], 1, 12))
    if (len({o.id for o in operations}) != len(operations) or len({o.key for o in operations}) != len(operations)
            or any(o.charge_limit_w > plant.charge_max_w or o.discharge_limit_w > plant.discharge_max_w for o in operations)):
        raise ValueError("duplicate or oversized native operation")
    reference = _id(wire["reference_id"])
    if reference not in {o.id for o in operations}:
        raise ValueError("common reference is missing")
    continuation = _obj(wire["continuation"], "representation coordinate_order cells")
    if (continuation["representation"] != "piecewise-quadratic-absolute-v1"
            or continuation["coordinate_order"] != ["energy_kwh", "previous_import_w"]):
        raise ValueError("unsupported continuation representation")
    cells = []
    for raw in _arr(continuation["cells"], 1, 64):
        _obj(raw, "id witness_id domain cost")
        cd = _obj(raw["domain"], "energy_kwh previous_import_w inequalities")
        er, pr = _range(cd["energy_kwh"]), _range(cd["previous_import_w"])
        if er[0] < plant.cutoff_kwh or er[1] > plant.capacity_kwh or pr[1] > plant.import_limit_w:
            raise ValueError("continuation domain exceeds physical limits")
        inequalities = []
        for row in _arr(cd["inequalities"], 0, 8):
            _obj(row, "energy previous_import maximum")
            inequalities.append(tuple(_num(row[k]) for k in ("energy", "previous_import", "maximum")))
        costs = _obj(raw["cost"], " ".join(COST_FIELDS))
        functions = []
        for key in COST_FIELDS:
            fn = _obj(costs[key], "polynomial absolute_terms")
            poly = tuple(_num(v) for v in _arr(fn["polynomial"], 3, 3))
            absolute = []
            for term in _arr(fn["absolute_terms"], 0, 2 if key == "ramp_sek" else 0):
                _obj(term, "weight energy previous_import constant")
                _num(term["weight"], 0)
                absolute.append(tuple(_num(term[k]) for k in ("weight", "energy", "previous_import", "constant")))
            functions.append(CostFunction(poly, tuple(absolute)))
        cell = ContinuationCell(_id(raw["id"]), _id(raw["witness_id"]), er, pr, tuple(inequalities), tuple(functions))
        # Validate nonnegative accounts at polynomial minima and ramp kinks too.
        points = {er[0], er[1]}
        for fn in functions:
            if fn.polynomial[2]:
                vertex = -fn.polynomial[1] / (2 * fn.polynomial[2])
                if er[0] < vertex < er[1]:
                    points.add(vertex)
            for _, e, p, constant in fn.absolute_terms:
                if e:
                    points.update(v for v in ((-p * bound - constant) / e for bound in pr) if er[0] <= v <= er[1])
        for e in points:
            for p in pr:
                cell.evaluate(e, p)
        cells.append(cell)
    if len({c.id for c in cells}) != len(cells):
        raise ValueError("duplicate continuation cell")
    q = _obj(wire["quality"], "assurance scorer_revision compiler_revision family_id source_hash numeric_tolerance_sek search_exhaustive_in_declared_graph search_pruned_prefixes heldout_count heldout_max_regret_sek certified_regret_bound_sek")
    if (q["assurance"] != "exact-scoring-within-published-family" or q["scorer_revision"] != "offline-household-v2"
            or q["compiler_revision"] != "execution-v2" or q["numeric_tolerance_sek"] != NUMERICAL_TOLERANCE_SEK
            or type(q["numeric_tolerance_sek"]) is bool or q["certified_regret_bound_sek"] is not None):
        raise ValueError("unsupported execution quality claim")
    exhaustive = _bool(q["search_exhaustive_in_declared_graph"])
    pruned = _int(q["search_pruned_prefixes"], 0, 40000000)
    count = _int(q["heldout_count"], 0, 40000000)
    regret = None if q["heldout_max_regret_sek"] is None else _num(q["heldout_max_regret_sek"], 0)
    if exhaustive != (pruned == 0) or ((count == 0) != (regret is None)):
        raise ValueError("inconsistent finite-family evidence")
    quality = PolicyQuality(_id(q["family_id"]), _id(q["source_hash"]), exhaustive, pruned, count, regret)
    return PolicySummary(identity, origin, start, refresh, until, boundary, domain, plant, permissions,
                         economics, reference, operations, tuple(cells), quality, supply_scope)


@dataclass(frozen=True)
class ExecutionPolicy:
    source_json: str

    def __post_init__(self):
        self.summary

    @cached_property
    def summary(self) -> PolicySummary:
        return _summary(self.source_json)


def read_execution_policy(data: bytes) -> ExecutionPolicy:
    if not isinstance(data, (bytes, str)) or len(data if isinstance(data, bytes) else data.encode()) > MAX_POLICY_BYTES:
        raise ValueError("execution policy exceeds 128KB")
    return ExecutionPolicy(canonical_json(read_runtime_json(data)))


def objective(values) -> Objective:
    result = dict.fromkeys(COMPONENTS, 0.0)
    result.update(values)
    result["billable_sek"] = result["import_sek"] - result["export_sek"]
    result["total_sek"] = result["billable_sek"] + sum(result[k] for k in ("wear_sek", "starts_sek", "shaping_sek", "ramp_sek")) - result["service_sek"] - result["terminal_sek"]
    if any(not isfinite(v) for v in result.values()):
        raise ValueError("nonfinite objective")
    return tuple(result[k] for k in COMPONENTS)


@dataclass(frozen=True)
class CurrentResponse:
    current: Objective
    energy_end_kwh: float
    terminal_import_w: float
    possible_import_w: float
    possible_export_w: float


@dataclass(frozen=True)
class OutsideCoverage:
    reason: str
    refresh_required: bool = True


def evaluate_current(policy: ExecutionPolicy, operation: BatteryOperation,
                     conditions: ExecutionConditions, now_ms: int) -> CurrentResponse | OutsideCoverage:
    """Exact steady native model over the remaining interval, split at saturation."""
    s, p, c = policy.summary, policy.summary.plant, conditions
    permissions, econ = s.permissions, s.economics
    if operation not in s.operations:
        raise ValueError("operation is not in this policy")
    if not s.from_ms <= now_ms < s.until_ms:
        return OutsideCoverage("outside_validity")
    if any(not bounds[0] <= value <= bounds[1] for value, bounds in (
            (c.energy_kwh, s.domain.energy_kwh), (c.pv_w, s.domain.pv_w), (c.residual_load_w, s.domain.residual_load_w))):
        return OutsideCoverage("outside_domain")
    op = operation.operation
    if not permissions.available and op != "hold":
        return OutsideCoverage("battery_unavailable")
    if op == "grid_charge" and not permissions.grid_charge_allowed:
        return OutsideCoverage("grid_charge_not_allowed")
    if op == "export":
        if not permissions.battery_export_allowed:
            return OutsideCoverage("battery_export_not_allowed")
        if not permissions.export_price_eligible or econ.export_sek_per_kwh < permissions.minimum_export_price_sek_per_kwh:
            return OutsideCoverage("export_price_ineligible")
    surplus = c.pv_w - c.residual_load_w
    charge = operation.charge_limit_w if op == "grid_charge" else min(operation.charge_limit_w, max(0, surplus)) if op in ("self_consumption", "solar_charge") else 0.0
    discharge = operation.discharge_limit_w if op == "export" else min(operation.discharge_limit_w, max(0, -surplus)) if op in ("self_consumption", "supply_house") else 0.0
    if s.supply_scope.kind == "selected" and c.eligible_load_w is None:
        return OutsideCoverage("scope_measurements_unavailable")
    eligible = (c.residual_load_w if s.supply_scope.kind == "whole_house" else
                0.0 if s.supply_scope.kind == "none" else c.eligible_load_w)
    bound = proportional_supply(c.residual_load_w, c.pv_w, eligible).house_supply_bound_w
    # Keep the catalog's commissioned operation intact. Clipping the response
    # without changing the native target would falsely claim enforcement.
    if min(discharge, max(0.0, -surplus)) > bound + PHYSICAL_TOLERANCE:
        return OutsideCoverage("native_operation_exceeds_supply_scope")
    hours = (s.boundary_ms - now_ms) / 3600000
    rate = (charge * p.charge_efficiency - discharge / p.discharge_efficiency) / 1000
    energy = c.energy_kwh
    if not p.cutoff_kwh <= energy <= p.capacity_kwh:
        return OutsideCoverage("energy_out_of_bounds")
    if op == "export":
        if energy <= permissions.export_reserve_kwh:
            return OutsideCoverage("export_reserve")
        if energy + rate * hours < permissions.export_reserve_kwh:
            return OutsideCoverage("export_reserve_crossing")
    active = hours
    if rate > 0:
        active = min(hours, max(0, (p.capacity_kwh - energy) / rate))
    elif rate < 0:
        active = min(hours, max(0, (energy - p.cutoff_kwh) / -rate))
    segments = []
    if active > 0:
        segments.append((active, charge, discharge))
    if active < hours:
        segments.append((hours - active, 0.0, 0.0))
    account = dict.fromkeys(COMPONENTS, 0.0)
    previous = c.previous_import_w
    for duration, charged, discharged in segments:
        imported = max(0, c.residual_load_w + charged - discharged - c.pv_w)
        exported = max(0, c.pv_w + discharged - charged - c.residual_load_w)
        if imported > p.import_limit_w + PHYSICAL_TOLERANCE or exported > p.export_limit_w + PHYSICAL_TOLERANCE:
            return OutsideCoverage("grid_limit")
        account["import_sek"] += imported / 1000 * duration * econ.import_sek_per_kwh
        account["export_sek"] += exported / 1000 * duration * econ.export_sek_per_kwh
        wear_w = discharged / p.discharge_efficiency if p.wear_basis == "discharged_storage" else charged + discharged
        account["wear_sek"] += wear_w / 1000 * duration * p.wear_sek_per_kwh
        account["shaping_sek"] += .5 * econ.shaping_sek_per_kwh_per_kw * (imported / 1000) ** 2 * duration
        account["ramp_sek"] += abs(imported - previous) / 1000 * econ.ramp_sek_per_kw
        previous = imported
    endpoint = max(p.cutoff_kwh, min(p.capacity_kwh, energy + rate * active))
    return CurrentResponse(objective(account), endpoint, previous,
                           operation.charge_limit_w if op == "grid_charge" else 0.0,
                           operation.discharge_limit_w if op == "export" else 0.0)


def evaluate_continuation(policy: ExecutionPolicy, energy_kwh: float, previous_import_w: float):
    options = []
    for cell in policy.summary.cells:
        value = cell.evaluate(energy_kwh, previous_import_w)
        if value is not None:
            options.append((value[-1], cell.id, cell.witness_id, value))
    if not options:
        return None
    best = min(option[0] for option in options)
    _, _, witness, value = min((option for option in options if option[0] <= best + NUMERICAL_TOLERANCE_SEK),
                               key=lambda option: option[1])
    return witness, value


@dataclass(frozen=True)
class RankedOperation:
    operation: BatteryOperation
    current: Objective
    continuation: Objective
    future_delta: Objective
    full: Objective
    total_delta_sek: float
    witness_id: str
    possible_import_w: float
    possible_export_w: float
    energy_end_kwh: float
    terminal_import_w: float


@dataclass(frozen=True)
class Decision:
    evaluated_at_ms: int
    valid_until_ms: int
    reference_id: str
    ranked: tuple[RankedOperation, ...]
    selected_id: str
    refresh_due: bool
    excluded: tuple[tuple[str, str], ...] = ()


def evaluate_policy(policy: ExecutionPolicy, conditions: ExecutionConditions, now_ms: int,
                    incumbent_id: Optional[str] = None, deadband_sek: float = .02) -> Decision | OutsideCoverage:
    _int(now_ms)
    _num(deadband_sek, 0, 1e6)
    s, c = policy.summary, conditions
    if c.identity != s.identity.context or c.permissions != s.permissions:
        return OutsideCoverage("context_mismatch")
    if not s.from_ms <= now_ms < s.until_ms:
        return OutsideCoverage("not_yet_valid" if now_ms < s.from_ms else "expired")
    if not c.at_ms <= now_ms < c.valid_until_ms:
        return OutsideCoverage("stale_conditions")
    if any(not bounds[0] <= value <= bounds[1] for value, bounds in (
            (c.energy_kwh, s.domain.energy_kwh), (c.pv_w, s.domain.pv_w), (c.residual_load_w, s.domain.residual_load_w))):
        return OutsideCoverage("conditions_outside_domain")
    rows, excluded = {}, []
    for op in s.operations:
        response = evaluate_current(policy, op, c, now_ms)
        if isinstance(response, OutsideCoverage):
            excluded.append((op.id, response.reason))
            continue
        future = evaluate_continuation(policy, response.energy_end_kwh, response.terminal_import_w)
        if future is None:
            excluded.append((op.id, "continuation_uncovered"))
            continue
        rows[op.id] = op, response, future
    if s.reference_id not in rows:
        return OutsideCoverage("reference_uncovered")
    _, ref_response, (_, ref_future) = rows[s.reference_id]
    ref_full = tuple(a + b for a, b in zip(ref_response.current, ref_future))
    ranked = []
    for op, response, (witness, future) in rows.values():
        full = tuple(a + b for a, b in zip(response.current, future))
        delta = tuple(a - b for a, b in zip(future, ref_future))
        ranked.append(RankedOperation(op, response.current, future, delta, full, full[-1] - ref_full[-1], witness,
                                      response.possible_import_w, response.possible_export_w,
                                      response.energy_end_kwh, response.terminal_import_w))
    ranked.sort(key=lambda row: (row.total_delta_sek, row.operation.id))
    best = ranked[0].total_delta_sek
    selected = min((r for r in ranked if r.total_delta_sek <= best + NUMERICAL_TOLERANCE_SEK), key=lambda r: r.operation.id)
    incumbent = next((r for r in ranked if r.operation.id == incumbent_id), None)
    if incumbent and incumbent.total_delta_sek <= selected.total_delta_sek + max(deadband_sek, NUMERICAL_TOLERANCE_SEK):
        selected = incumbent
    return Decision(now_ms, s.until_ms, s.reference_id, tuple(ranked), selected.operation.id,
                    now_ms >= s.refresh_after_ms, tuple(excluded))
