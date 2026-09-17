"""Planner-owned battery reference and receipt-ordered execution accounting.

No services, prices, horizon search or inferred meter delivery live here. The
account is an immutable evidence journal; balances and objective outcomes are
read models. All energies are integer mWh, all times UTC milliseconds.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from fractions import Fraction
from math import ceil, isfinite
from typing import Optional

if __package__:
    from .battery_conversion import Conversion
    from .battery_supply import proportional_supply
else:
    from battery_conversion import Conversion
    from battery_supply import proportional_supply

SCHEMA = "battery-plan-execution-v1"


def _integer(value, *, minimum=None):
    if type(value) is not int or abs(value) > 2**53 - 1 or (minimum is not None and value < minimum):
        raise ValueError("expected an exact integer on the declared accounting basis")
    return value


def _text(value):
    if type(value) is not str or not value:
        raise ValueError("explicit identity or reason required")
    return value


def _fields(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise ValueError("unknown or missing execution contract fields")
    return value


def _round(value: Fraction) -> int:
    """Nearest mWh, ties toward +infinity, matching the producer's Math.round."""
    return (value + Fraction(1, 2)).__floor__()


@dataclass(frozen=True)
class Bounds:
    low: int
    high: Optional[int]

    def __post_init__(self):
        _integer(self.low)
        if self.high is not None and _integer(self.high) < self.low:
            raise ValueError("reversed energy bounds")

    @property
    def exact(self):
        return self.high == self.low

    def plus(self, other):
        return Bounds(self.low + other.low, None if self.high is None or other.high is None else self.high + other.high)


@dataclass(frozen=True)
class ReferenceInterval:
    start_ms: int
    end_ms: int
    stored_start_mwh: int
    stored_end_mwh: int
    operation: str
    target_kind: str
    grid_charge_allowed: bool
    export_allowed: bool
    charge_ac_limit_w: int
    discharge_ac_limit_w: int
    follows_demand: bool
    charge_ac_mwh: int
    discharge_ac_mwh: int
    pv_mwh: int
    load_mwh: int
    import_mwh: int
    export_mwh: int
    curtailed_mwh: int
    unserved_mwh: int
    rounding_mwh: int

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key.endswith("_ms") or key.endswith("_mwh") or key.endswith("_w"):
                _integer(value, minimum=None if key == "rounding_mwh" else 0)
        if self.end_ms <= self.start_ms:
            raise ValueError("reference interval must have positive duration")
        if self.operation not in ("hold", "grid_charge", "solar_charge", "supply_house", "export"):
            raise ValueError("unsupported nominal battery role")
        if self.target_kind not in ("stored_energy", "demand_following", "permission"):
            raise ValueError("target, forecast and permission must be distinguished")
        if any(type(v) is not bool for v in (self.grid_charge_allowed, self.export_allowed, self.follows_demand)):
            raise ValueError("explicit source/export permissions required")
        if self.operation == "grid_charge" and not self.grid_charge_allowed:
            raise ValueError("nominal operation lacks grid permission")
        if self.operation == "export" and not self.export_allowed:
            raise ValueError("nominal operation lacks export permission")
        if self.charge_ac_mwh and self.discharge_ac_mwh:
            raise ValueError("nominal charge and discharge cannot coincide")
        if self.import_mwh and self.export_mwh:
            raise ValueError("nominal import and export cannot coincide")
        # Eight independently rounded gross terms have at most 4 mWh residual.
        residual = (self.pv_mwh + self.import_mwh + self.discharge_ac_mwh
                    - self.load_mwh + self.unserved_mwh - self.charge_ac_mwh
                    - self.export_mwh - self.curtailed_mwh)
        if residual != self.rounding_mwh or abs(residual) > 4:
            raise ValueError("nominal electrical account does not conserve energy")
        if self.unserved_mwh > self.load_mwh:
            raise ValueError("unserved energy exceeds total demand")
        if (self.operation == "hold" and (self.charge_ac_mwh or self.discharge_ac_mwh)
                or self.operation in ("grid_charge", "solar_charge") and self.discharge_ac_mwh
                or self.operation in ("supply_house", "export") and self.charge_ac_mwh):
            raise ValueError("nominal role contradicts directional flows")

    def stored_at(self, at_ms):
        elapsed = min(self.end_ms, max(self.start_ms, at_ms)) - self.start_ms
        return self.stored_start_mwh + _round(Fraction(
            (self.stored_end_mwh - self.stored_start_mwh) * elapsed,
            self.end_ms - self.start_ms))


@dataclass(frozen=True)
class Objective:
    id: str
    kind: str
    start_ms: int
    deadline_ms: int
    target_mwh: int
    reason: str

    def __post_init__(self):
        _text(self.id); _text(self.reason)
        for value in (self.start_ms, self.deadline_ms, self.target_mwh):
            _integer(value, minimum=0)
        if self.kind not in ("stored_energy", "demand_following", "permission") or self.deadline_ms <= self.start_ms:
            raise ValueError("objective needs a typed target and original deadline")


@dataclass(frozen=True)
class Recovery:
    objective_id: str
    start_ms: int
    end_ms: int
    max_correction_mwh: int
    extra_charge_dc_w: int
    order: int
    resource_id: str
    rule: str
    reason: str

    def __post_init__(self):
        for value in (self.objective_id, self.resource_id, self.reason):
            _text(value)
        for value in (self.start_ms, self.end_ms, self.max_correction_mwh, self.extra_charge_dc_w, self.order):
            _integer(value, minimum=0)
        if self.end_ms <= self.start_ms or self.rule != "nominal_first_then_earliest":
            raise ValueError("recovery needs explicit timing and shared allocation")


@dataclass(frozen=True)
class Disposition:
    objective_id: str
    outcome: str
    replacement_id: Optional[str]
    reason: str

    def __post_init__(self):
        _text(self.objective_id); _text(self.reason)
        if self.outcome not in ("retained", "incorporated", "retired"):
            raise ValueError("planner cannot manufacture measured fulfilment")
        if (self.outcome == "incorporated") != (self.replacement_id is not None):
            raise ValueError("incorporation needs exactly one replacement objective")
        if self.replacement_id is not None:
            _text(self.replacement_id)


@dataclass(frozen=True)
class ExecutionContract:
    id: str
    plan_id: str
    generation: int
    mode: str
    scope_revision: str
    model_revision: str
    energy_basis: str
    capacity_mwh: int
    minimum_mwh: int
    maximum_mwh: int
    export_reserve_mwh: int
    valid_until_ms: int
    source_receipt: int
    previous_contract_id: Optional[str]
    intervals: tuple[ReferenceInterval, ...]
    objectives: tuple[Objective, ...]
    recovery: tuple[Recovery, ...]
    dispositions: tuple[Disposition, ...]

    def __post_init__(self):
        for value in (self.id, self.plan_id, self.scope_revision, self.model_revision):
            _text(value)
        if self.energy_basis != "stored_energy_mwh" or self.mode not in ("controlling", "control_verification"):
            raise ValueError("unsupported accounting basis or execution mode")
        for value in (self.generation, self.capacity_mwh, self.minimum_mwh, self.maximum_mwh, self.export_reserve_mwh, self.valid_until_ms, self.source_receipt):
            _integer(value, minimum=0)
        if not self.minimum_mwh < self.maximum_mwh <= self.capacity_mwh or not self.intervals:
            raise ValueError("battery reference requires capacity and intervals")
        if not self.minimum_mwh <= self.export_reserve_mwh <= self.capacity_mwh:
            raise ValueError("export reserve must lie inside physical storage limits")
        if self.previous_contract_id is not None:
            _text(self.previous_contract_id)
        if not self.intervals[0].start_ms < self.valid_until_ms <= self.intervals[-1].end_ms:
            raise ValueError("validity must lie inside the reference")
        for i, row in enumerate(self.intervals):
            if any(not 0 <= v <= self.capacity_mwh for v in (row.stored_start_mwh, row.stored_end_mwh)):
                raise ValueError("reference exceeds physical storage capacity")
            if i and (row.start_ms != self.intervals[i-1].end_ms or row.stored_start_mwh != self.intervals[i-1].stored_end_mwh):
                raise ValueError("reference intervals and state must be contiguous")
        ids = {o.id for o in self.objectives}
        if len(ids) != len(self.objectives) or len({d.objective_id for d in self.dispositions}) != len(self.dispositions):
            raise ValueError("objective and disposition identities must be unique")
        for objective in self.objectives:
            lower, upper = (self.minimum_mwh, self.maximum_mwh) if objective.kind == "stored_energy" else (0, self.capacity_mwh)
            if not lower <= objective.target_mwh <= upper:
                raise ValueError("objective exceeds physical storage capacity")
            if not self.intervals[0].start_ms <= objective.start_ms < objective.deadline_ms <= self.intervals[-1].end_ms:
                raise ValueError("objective timing is outside its reference")
        for d in self.dispositions:
            if d.replacement_id is not None and d.replacement_id not in ids:
                raise ValueError("incorporation names an absent objective")
        for r in self.recovery:
            objective = next((o for o in self.objectives if o.id == r.objective_id), None)
            if objective is None or objective.kind != "stored_energy" or not objective.start_ms <= r.start_ms < r.end_ms <= objective.deadline_ms:
                raise ValueError("recovery must respect its original objective deadline")
        # One battery cannot spend the same delegated headroom on two objectives.
        for i, r in enumerate(self.recovery):
            if any(max(r.start_ms, other.start_ms) < min(r.end_ms, other.end_ms) for other in self.recovery[:i]):
                raise ValueError("overlapping battery recovery allocations")

    def interval(self, at_ms):
        return next((r for r in self.intervals if r.start_ms <= at_ms < min(r.end_ms, self.valid_until_ms)), None)

    def stored_at(self, at_ms):
        if not self.intervals[0].start_ms <= at_ms <= self.intervals[-1].end_ms:
            raise ValueError("reference does not cover accounting instant")
        return next(r for r in self.intervals if at_ms <= r.end_ms).stored_at(at_ms)


def read_contract(value):
    raw = dict(_fields(value, "schema id plan_id generation mode scope_revision model_revision energy_basis capacity_mwh minimum_mwh maximum_mwh export_reserve_mwh valid_until_ms source_receipt previous_contract_id intervals objectives recovery dispositions"))
    if raw.pop("schema") != SCHEMA:
        raise ValueError("unsupported execution contract; no legacy translation")
    for field, cls in (("intervals", ReferenceInterval), ("objectives", Objective), ("recovery", Recovery), ("dispositions", Disposition)):
        if type(raw[field]) is not list:
            raise ValueError("execution records must be arrays")
        raw[field] = tuple(cls(**_fields(v, " ".join(cls.__dataclass_fields__))) for v in raw[field])
    return ExecutionContract(**raw)


def contract_wire(contract):
    value = asdict(contract)
    for field in ("intervals", "objectives", "recovery", "dispositions"):
        value[field] = list(value[field])
    return {"schema": SCHEMA, **value}


@dataclass(frozen=True)
class MeterReceipt:
    event_id: str
    stream: str
    direction: str
    boundary: str
    epoch: str
    source_at_ms: int
    total_mwh: int
    receipt: int

    def __post_init__(self):
        for value in (self.event_id, self.stream, self.epoch):
            _text(value)
        for value in (self.source_at_ms, self.total_mwh, self.receipt):
            _integer(value, minimum=0)
        if (self.boundary, self.direction) not in (("battery_dc", "charge"), ("battery_dc", "discharge"), ("grid_ac", "import"), ("grid_ac", "export")):
            raise ValueError("explicit supported meter boundary/direction required")


def measured(receipts, direction, start_ms, end_ms):
    """Interval evidence, with later receipts correcting earlier source instants.

    Ordering the measurement *intervals* is not ordering event admission. A
    regressed/equal source time is accepted and can amend retained history. No
    power integration, proportional allocation, or command acknowledgement is
    ever used to manufacture measured energy.
    """
    if end_ms < start_ms:
        raise ValueError("reversed accounting interval")
    if end_ms == start_ms:
        return Bounds(0, 0)
    rows = [r for r in receipts if r.direction == direction]
    if not rows:
        return Bounds(0, None)
    streams = {r.stream for r in rows}
    result = Bounds(0, 0)
    for stream in streams:
        latest = {}
        for r in rows:  # Receipt order is authoritative for corrections.
            if r.stream == stream:
                latest[r.source_at_ms] = r
        samples = sorted(latest.values(), key=lambda r: r.source_at_ms)
        low = high = covered = 0
        uncertain = False
        for a, b in zip(samples, samples[1:]):
            left, right = max(start_ms, a.source_at_ms), min(end_ms, b.source_at_ms)
            if left >= right:
                continue
            covered += right - left
            if a.epoch != b.epoch or b.total_mwh < a.total_mwh:
                uncertain = True
                continue
            delta = b.total_mwh - a.total_mwh
            high += delta
            if left == a.source_at_ms and right == b.source_at_ms:
                low += delta
        result = result.plus(Bounds(low, None if uncertain or covered != end_ms - start_ms else high))
    return result


@dataclass(frozen=True)
class Admission:
    contract: ExecutionContract
    at_ms: int
    reference_adjustment_mwh: int
    receipt: int

    def __post_init__(self):
        for value in (self.at_ms, self.receipt):
            _integer(value, minimum=0)
        _integer(self.reference_adjustment_mwh)
        if self.contract.interval(self.at_ms) is None:
            raise ValueError("admission lies outside contract validity")


@dataclass(frozen=True)
class StateObservation:
    at_ms: int
    stored_mwh: int
    source: str

    def __post_init__(self):
        _integer(self.at_ms, minimum=0); _integer(self.stored_mwh, minimum=0); _text(self.source)


@dataclass(frozen=True)
class RequestAnchor:
    generation: int
    source_receipt: int

    def __post_init__(self):
        _integer(self.generation, minimum=1); _integer(self.source_receipt, minimum=0)


@dataclass(frozen=True)
class Account:
    requested_generation: int = 0
    receipt: int = 0
    admissions: tuple[Admission, ...] = ()
    meters: tuple[MeterReceipt, ...] = ()
    opening: Optional[StateObservation] = None
    observations: tuple[StateObservation, ...] = ()
    requests: tuple[RequestAnchor, ...] = ()

    def __post_init__(self):
        _integer(self.requested_generation, minimum=0); _integer(self.receipt, minimum=0)
        records = (*self.meters, *self.admissions)
        if len({r.receipt for r in records}) != len(records) or any(r.receipt > self.receipt for r in records):
            raise ValueError("journal receipts must be unique and within the accepted prefix")
        if any(b.receipt <= a.receipt for a, b in zip(self.meters, self.meters[1:])):
            raise ValueError("meter journal must preserve local receipt order")
        if self.admissions and (self.opening is None or self.opening.at_ms != self.admissions[0].at_ms):
            raise ValueError("reference history must retain its opening physical anchor")
        for before, after in zip(self.admissions, self.admissions[1:]):
            if (after.receipt <= before.receipt or after.at_ms < before.at_ms
                    or after.contract.generation <= before.contract.generation
                    or after.contract.previous_contract_id != before.contract.id):
                raise ValueError("reference history has a broken local handover chain")
        if self.admissions and self.admissions[-1].contract.generation > self.requested_generation:
            raise ValueError("accepted response exceeds local request generation")
        if self.requests and (self.requests[-1].generation != self.requested_generation or
                any(r.source_receipt > self.receipt for r in self.requests)):
            raise ValueError("request snapshot differs from local receipt history")

    @property
    def contract(self):
        return self.admissions[-1].contract if self.admissions else None

    @property
    def observed(self):
        return self.observations[-1] if self.observations else None


def request_replan(account):
    """Reserve a local generation before sending; network results carry it back."""
    generation = account.requested_generation + 1
    return replace(account, requested_generation=generation,
                   requests=(*account.requests, RequestAnchor(generation, account.receipt)))


def observe_state(account, observation):
    # Source time never decides receipt order. Interval time remains provenance.
    return replace(account, receipt=account.receipt + 1, observations=(*account.observations, observation))


def record_meter(account, *, event_id, stream, direction, boundary, epoch, source_at_ms, total_mwh):
    receipt = MeterReceipt(event_id, stream, direction, boundary, epoch, source_at_ms, total_mwh, account.receipt + 1)
    existing = next((r for r in account.meters if r.event_id == event_id), None)
    if existing:
        if replace(existing, receipt=receipt.receipt) != receipt:
            raise ValueError("one meter event identity cannot describe conflicting evidence")
        return account
    previous = [r for r in account.meters if r.stream == stream]
    if previous and (previous[-1].boundary, previous[-1].direction) != (boundary, direction):
        raise ValueError("meter mapping changes need a distinct physical stream")
    if any(r.stream != stream and r.boundary == boundary and r.direction == direction for r in account.meters):
        raise ValueError("one declared physical boundary cannot be counted twice")
    return replace(account, receipt=receipt.receipt, meters=(*account.meters, receipt))


def admit_plan(account, contract, at_ms, observation):
    """Atomic admission at a common instant, without rebasing physical actuals."""
    _integer(at_ms, minimum=0)
    old = account.contract
    if old and contract.id == old.id:
        if contract != old:
            raise ValueError("an accepted reference revision is immutable")
        return account
    if contract.generation != account.requested_generation or (old and contract.generation <= old.generation):
        raise ValueError("response belongs to a superseded local request generation")
    if contract.source_receipt > account.receipt:
        raise ValueError("planner acknowledges evidence that was never received")
    anchor = next((r for r in account.requests if r.generation == contract.generation), None)
    if anchor and contract.source_receipt != anchor.source_receipt:
        raise ValueError("planner acknowledgement differs from the request's actuals prefix")
    if old and contract.previous_contract_id != old.id:
        raise ValueError("replacement must identify the accepted reference")
    if account.admissions and at_ms < account.admissions[-1].at_ms:
        raise ValueError("activation must use the local acceptance clock")
    if not old and contract.previous_contract_id is not None:
        raise ValueError("initial contract names an unknown reference")
    if contract.interval(at_ms) is None:
        raise ValueError("contract does not authorise the activation instant")
    if observation.at_ms != at_ms:
        raise ValueError("activation requires an observation captured at acceptance")
    if not 0 <= observation.stored_mwh <= contract.capacity_mwh:
        raise ValueError("observed state is outside the declared battery capacity")
    if old and (old.energy_basis, old.capacity_mwh, old.minimum_mwh, old.maximum_mwh) != (contract.energy_basis, contract.capacity_mwh, contract.minimum_mwh, contract.maximum_mwh):
        raise ValueError("changed storage basis requires an explicit state reconciliation")
    previous = old.stored_at(min(at_ms, old.valid_until_ms)) if old else contract.stored_at(at_ms)
    amendment = contract.stored_at(at_ms) - previous
    # Omitted dispositions never erase an outcome; objective_history retains it.
    history_ids = {o.id for a in account.admissions for o in a.contract.objectives}
    if any(d.objective_id not in history_ids for d in contract.dispositions):
        raise ValueError("disposition names an unknown prior objective")
    if old:
        old_objectives = {o.id: o for a in account.admissions for o in a.contract.objectives}
        for objective in contract.objectives:
            prior = old_objectives.get(objective.id)
            if prior and (prior.kind, prior.deadline_ms) != (objective.kind, objective.deadline_ms):
                raise ValueError("stable objective identity cannot move its original deadline")
            if prior and prior.target_mwh != objective.target_mwh:
                if at_ms >= prior.deadline_ms:
                    raise ValueError("a planner amendment cannot rewrite a past deadline target")
                if not any(d.objective_id == objective.id and d.outcome == "retained" for d in contract.dispositions):
                    raise ValueError("changed objective target needs an explicit retained amendment")
    number = account.receipt + 1
    return replace(account, receipt=number, opening=account.opening or observation,
                   observations=(*account.observations, observation),
                   admissions=(*account.admissions, Admission(contract, at_ms, amendment, number)))


@dataclass(frozen=True)
class Balance:
    reference_mwh: int
    charge: Bounds
    discharge: Bounds
    flow_debt_low_mwh: Optional[int]
    flow_debt_high_mwh: Optional[int]
    state_debt_mwh: Optional[int]
    state_residual_low_mwh: Optional[int]
    state_residual_high_mwh: Optional[int]


def balance(account, at_ms):
    if not account.admissions or account.opening is None:
        raise ValueError("account has no accepted reference and opening state")
    admission = next((a for a in reversed(account.admissions) if a.at_ms <= at_ms), None)
    if admission is None:
        raise ValueError("accounting instant precedes admission")
    contract = admission.contract
    reference = contract.stored_at(min(at_ms, contract.valid_until_ms))
    charge = measured(account.meters, "charge", account.opening.at_ms, at_ms)
    discharge = measured(account.meters, "discharge", account.opening.at_ms, at_ms)
    origin = account.opening.stored_mwh
    lo = None if charge.high is None else reference - origin - charge.high + discharge.low
    hi = None if discharge.high is None else reference - origin - charge.low + discharge.high
    observation = next((o for o in reversed(account.observations) if o.at_ms == at_ms), None)
    state_debt = reference - observation.stored_mwh if observation else None
    return Balance(reference, charge, discharge, lo, hi, state_debt,
                   None if lo is None or state_debt is None else lo - state_debt,
                   None if hi is None or state_debt is None else hi - state_debt)


def objective_history(account, at_ms):
    """Planner dispositions and measured outcomes remain distinct and replayable."""
    records = {}
    for admission in account.admissions:
        if admission.at_ms > at_ms:
            continue
        for objective in admission.contract.objectives:
            row = records.setdefault(objective.id, {"objective": asdict(objective), "origin_contract_id": admission.contract.id,
                                                    "versions": [], "dispositions": []})
            row["versions"].append({"objective": asdict(objective), "contract_id": admission.contract.id, "at_ms": admission.at_ms})
            if admission.at_ms <= at_ms:
                row["objective"] = asdict(objective)
        for disposition in admission.contract.dispositions:
            records[disposition.objective_id]["dispositions"].append({**asdict(disposition),
                "contract_id": admission.contract.id, "at_ms": admission.at_ms})
    for row in records.values():
        objective = row["objective"]
        deadline = objective["deadline_ms"]
        row["outcome"] = "open"
        row["shortfall_mwh"] = None
        row["fulfilment_basis"] = None
        closed = next((d for d in row["dispositions"] if d["outcome"] in ("incorporated", "retired")), None)
        if closed and closed["at_ms"] < deadline:
            row["outcome"] = "changed_before_deadline"
        elif objective["kind"] == "stored_energy" and deadline <= at_ms and account.opening.at_ms <= deadline:
            charged = measured(account.meters, "charge", account.opening.at_ms, deadline)
            discharged = measured(account.meters, "discharge", account.opening.at_ms, deadline)
            if charged.exact and discharged.exact:
                shortfall = max(0, objective["target_mwh"] - account.opening.stored_mwh - charged.low + discharged.low)
                row.update(outcome="missed" if shortfall else "fulfilled", shortfall_mwh=shortfall,
                           fulfilment_basis="accounted_stored_energy", flow_shortfall_mwh=shortfall)
            else:
                row["outcome"] = "unresolved"
            observed = next((o for o in reversed(account.observations) if o.at_ms == deadline), None)
            if observed:
                shortfall = max(0, objective["target_mwh"] - observed.stored_mwh)
                row.update(outcome="missed" if shortfall else "fulfilled", shortfall_mwh=shortfall,
                           fulfilment_basis="observed_stored_energy")
        elif deadline <= at_ms:
            row["outcome"] = "forecast_complete" if objective["kind"] != "stored_energy" else "unresolved"
        last = row["dispositions"][-1] if row["dispositions"] else None
        row["responsibility"] = last["outcome"] if last else "outstanding"
        # Historical measured success/miss is never replaced by 'incorporated'.
    return tuple(records.values())


def feedback(account, at_ms):
    return {"generation": account.requested_generation, "source_receipt": account.receipt,
            "previous_contract_id": account.contract.id if account.contract else None,
            "observed": asdict(account.observed) if account.observed else None,
            "balance": asdict(balance(account, at_ms)) if account.contract else None,
            "objectives": list(objective_history(account, at_ms)) if account.contract else []}


def capture_replan(account, at_ms):
    """Capture generation and its complete evidence payload in one transition.

    The host persists the returned account and sends this already captured
    payload as an effect. Later receipts cannot change the request's prefix.
    """
    updated = request_replan(account)
    return updated, feedback(updated, at_ms)


@dataclass(frozen=True)
class LiveState:
    at_ms: int
    stored_mwh: int
    house_w: float
    pv_w: float
    eligible_load_w: float
    charge_max_dc_w: int
    discharge_max_dc_w: int
    import_limit_w: float
    export_limit_w: float
    pending_import_w: float = 0
    available: bool = True
    grid_charge_allowed: bool = True
    export_allowed: bool = False
    export_reserve_mwh: int = 0

    def __post_init__(self):
        for value in (self.at_ms, self.stored_mwh, self.charge_max_dc_w, self.discharge_max_dc_w, self.export_reserve_mwh):
            _integer(value, minimum=0)
        for value in (self.house_w, self.pv_w, self.eligible_load_w, self.import_limit_w,
                      self.export_limit_w, self.pending_import_w):
            if type(value) not in (int, float) or not isfinite(value) or value < 0:
                raise ValueError("live power/capability must be finite and nonnegative")
        if self.eligible_load_w > self.house_w:
            raise ValueError("eligible gross demand exceeds measured household demand")
        if any(type(v) is not bool for v in (self.available, self.grid_charge_allowed, self.export_allowed)):
            raise ValueError("live permissions must be explicit")


@dataclass(frozen=True)
class Assessment:
    operation: str
    charge_dc_w: int
    discharge_dc_w: int
    nominal_charge_dc_w: int
    recovery_dc_w: int
    reason: str
    recovery_state: str
    recovery_remaining_mwh: Optional[int]
    replan_reason: Optional[str]
    valid_until_ms: int


def assess_execution(account: Account, live: LiveState, conversion: Conversion) -> Assessment:
    """Follow the accepted role; delegate no price comparison to the executor."""
    contract, now = account.contract, live.at_ms
    if contract is None or (row := contract.interval(now)) is None:
        return Assessment("hold", 0, 0, 0, 0, "no_current_plan", "unavailable", None, "plan_required", now)
    until = min(row.end_ms, contract.valid_until_ms)
    if not live.available:
        return Assessment("hold", 0, 0, 0, 0, "equipment_unavailable", "unavailable", None, "equipment_unavailable", until)
    if not 0 <= live.stored_mwh <= contract.capacity_mwh:
        return Assessment("hold", 0, 0, 0, 0, "storage_limit", "unavailable", None, "state_outside_capacity", until)
    nominal = extra = charge = discharge = 0
    operation, reason, recovery_state, replan, remaining = row.operation, "following_plan", "not_needed", None, None
    surplus = max(0.0, live.pv_w - live.house_w)
    if operation in ("grid_charge", "solar_charge"):
        if operation == "grid_charge" and not (row.grid_charge_allowed and live.grid_charge_allowed):
            operation, reason, replan = "solar_charge", "grid_source_restricted", "source_permission_changed"
        if operation == "solar_charge":
            nominal = min(live.charge_max_dc_w, int(conversion.solar_capacity(live.pv_w, live.house_w)))
        else:
            # Nominal energy defines a constant AC request. It is converted with
            # the retained empirical model, rather than mistaking AC watts for
            # terminal watts or applying a nominal 95% to actual DC counters.
            ac_w = row.charge_ac_mwh * 3600 / (row.end_ms - row.start_ms)
            solar_ac = min(ac_w, surplus)
            nominal = min(live.charge_max_dc_w, int(conversion.surplus_charge.output(solar_ac)
                          + conversion.grid_charge.output(max(0, ac_w - solar_ac))))
        b = balance(account, now)
        # A state-only discrepancy never fabricates flow debt. Only a proven
        # common positive shortfall can consume discretionary recovery authority.
        state_debt = row.stored_at(now) - live.stored_mwh
        debt = max(0, min(state_debt, b.flow_debt_low_mwh)) if b.flow_debt_low_mwh is not None else None
        remaining = debt
        recipes = sorted((r for r in contract.recovery if r.start_ms <= now < r.end_ms), key=lambda r: r.order)
        if debt:
            recovery_state = "authorised" if recipes else "needs_replan"
            if recipes:
                recipe = recipes[0]
                target = next(o.target_mwh for o in contract.objectives if o.id == recipe.objective_id)
                charged = measured(account.meters, "charge", recipe.start_ms, now)
                planned = max(0, contract.stored_at(now) - contract.stored_at(recipe.start_ms))
                used = None if charged.high is None else max(0, charged.high - planned)
                quantity = 0 if used is None else min(debt, max(0, recipe.max_correction_mwh - used), max(0, target - live.stored_mwh))
                extra = min(recipe.extra_charge_dc_w, max(0, live.charge_max_dc_w - nominal)) if quantity else 0
                if extra:
                    until = min(until, recipe.end_ms, now + ceil(quantity * 3600 / extra))
                recovery_state = "projected_feasible" if quantity else "waiting_for_measurements"
            else:
                replan = "shortfall_without_recovery_authority"
        elif debt is None and state_debt > 0:
            recovery_state = "waiting_for_measurements"
        for objective in contract.objectives:
            if objective.kind != "stored_energy" or not objective.start_ms <= now < objective.deadline_ms:
                continue
            nominal_remaining = sum(max(0, r.stored_at(min(r.end_ms, objective.deadline_ms)) - r.stored_at(max(now, r.start_ms)))
                                    for r in contract.intervals if r.end_ms > now and r.start_ms < objective.deadline_ms)
            delegated = sum(min(r.max_correction_mwh, r.extra_charge_dc_w * max(0, r.end_ms - max(now, r.start_ms)) // 3600)
                            for r in contract.recovery if r.objective_id == objective.id and r.end_ms > now)
            if objective.target_mwh - live.stored_mwh > nominal_remaining + delegated:
                recovery_state, replan = "insufficient_capacity", "observed_state_cannot_meet_target"
        # Respect the instantaneous physical import budget, including possible
        # effects of real commands to other devices. Forecast error is no gate.
        desired = min(live.charge_max_dc_w, nominal + extra)
        lo, hi = 0, desired
        while lo < hi:
            mid = (lo + hi + 1) // 2
            net = conversion.net_grid(mid, 0, live.pv_w, live.house_w) + live.pending_import_w
            if net <= live.import_limit_w:
                lo = mid
            else:
                hi = mid - 1
        charge = lo
        if live.stored_mwh >= contract.maximum_mwh:
            charge, reason = 0, "battery_full"
        elif any(o.kind == "stored_energy" and o.start_ms <= now < o.deadline_ms
                 and live.stored_mwh >= o.target_mwh for o in contract.objectives):
            charge, reason = 0, "stored_target_reached"
        elif charge < desired:
            reason = "import_headroom_reduced"
        if extra:
            extra = max(0, charge - nominal)
            recovery_state = "executing" if extra else "temporarily_limited"
        # Corrections cannot be advertised as feasible once their remaining
        # delegated capacity cannot cover the known debt before its deadline.
        if debt and recipes:
            recipe = recipes[0]
            capacity, known = 0, True
            for r in contract.recovery:
                if r.objective_id != recipe.objective_id or r.end_ms <= now:
                    continue
                used = 0
                if r.start_ms < now:
                    delivered = measured(account.meters, "charge", r.start_ms, now)
                    if delivered.high is None:
                        known = False
                        continue
                    used = max(0, delivered.high - max(0, contract.stored_at(now) - contract.stored_at(r.start_ms)))
                capacity += min(max(0, r.max_correction_mwh - used),
                                r.extra_charge_dc_w * max(0, r.end_ms - max(now, r.start_ms)) // 3600)
            if known and capacity < debt:
                recovery_state, replan = "insufficient_capacity", "recovery_cannot_meet_deadline"
    elif operation in ("supply_house", "export"):
        scope = proportional_supply(live.house_w, live.pv_w, live.eligible_load_w).house_supply_bound_w
        if operation == "export" and not (row.export_allowed and live.export_allowed):
            operation, reason, replan = "hold", "export_restricted", "export_permission_changed"
        else:
            ac = scope if row.follows_demand else min(scope, row.discharge_ac_limit_w)
            if operation == "export":
                # The native export ceiling includes house supply. PV export
                # already present consumes the physical export limit first.
                ac = min(row.discharge_ac_limit_w, scope + max(0, live.export_limit_w - surplus))
                if scope < max(0, live.house_w - live.pv_w) and ac > scope:
                    ac, reason, replan = scope, "supply_scope_restricted", "export_cannot_respect_supply_scope"
            discharge = min(live.discharge_max_dc_w, int(conversion.discharge.input(ac)))
            reserve = max(contract.minimum_mwh, contract.export_reserve_mwh, live.export_reserve_mwh) if operation == "export" else contract.minimum_mwh
            if live.stored_mwh <= reserve:
                discharge, reason = 0, "battery_reserve"
            elif discharge:
                until = min(until, now + ceil((live.stored_mwh - reserve) * 3600 / discharge))
    if not charge and not discharge:
        operation = "hold"
    outstanding = objective_history(account, now)
    if any(r["outcome"] == "missed" and r["responsibility"] in ("outstanding", "retained") for r in outstanding):
        replan = replan or "objective_missed"
    active_ids = {o.id for o in contract.objectives}
    if any(r["objective"]["id"] not in active_ids and r["responsibility"] in ("outstanding", "retained") for r in outstanding):
        replan = replan or "handover_disposition_missing"
    return Assessment(operation, charge, discharge, nominal, extra, reason, recovery_state, remaining, replan, until)


def explain_execution(account: Account, live: LiveState, assessment: Assessment, *, measured_battery_dc_w=None):
    """Content for the existing battery card, separate from its layout.

    Native requests never become statements of delivered power. The UI formats
    the explicit deadline in the home's timezone, using its existing date helper.
    """
    contract = account.contract
    row = contract.interval(live.at_ms) if contract else None
    plans = {"hold": "The plan is to keep energy in the battery for later.",
             "grid_charge": "The plan is to charge the battery now for later use.",
             "solar_charge": "The plan is to store spare solar energy.",
             "supply_house": "The plan is to use the battery to help power your home.",
             "export": "The plan is to sell energy from the battery to the grid."}
    status = "Following the plan"
    if contract and contract.mode == "control_verification":
        status = "Testing the plan; battery settings are not being changed"
    elif assessment.replan_reason:
        status = "A revised plan is needed"
    elif assessment.reason == "battery_full":
        status = "The battery is full"
    elif assessment.reason == "stored_target_reached":
        status = "The planned charge level has been reached"
    elif assessment.reason == "import_headroom_reduced":
        status = "Charging is reduced to leave enough power for your home"
    now = f"Your home is using {live.house_w / 1000:.2f} kW. Solar is providing {live.pv_w / 1000:.2f} kW."
    if measured_battery_dc_w is None:
        now += " Waiting for a battery power reading."
    elif not isinstance(measured_battery_dc_w, (int, float)) or not isfinite(measured_battery_dc_w):
        raise ValueError("card needs a real finite battery measurement")
    elif measured_battery_dc_w > 0:
        now += f" The battery is charging at {measured_battery_dc_w / 1000:.2f} kW."
    elif measured_battery_dc_w < 0:
        now += f" The battery is supplying {-measured_battery_dc_w / 1000:.2f} kW."
    else:
        now += " The battery is neither charging nor supplying power."
    difference = "Waiting for an accepted plan to compare with."
    if row:
        delta = row.stored_at(live.at_ms) - live.stored_mwh
        difference = (f"The battery has {abs(delta) / 1e6:.2f} kWh {'less' if delta > 0 else 'more'} stored than the plan expected."
                      if delta else "The stored energy matches the plan.")
    next_action = "Continue with the current plan."
    deadline = None
    if assessment.replan_reason:
        next_action = "Ask the planner to account for the remaining energy need."
    elif assessment.recovery_state == "executing":
        next_action = ("In control mode, SHS would request extra charging to catch up."
                       if contract and contract.mode == "control_verification" else
                       "Extra charging is being requested to catch up. Energy readings will confirm the result.")
    elif assessment.recovery_state in ("authorised", "projected_feasible", "temporarily_limited"):
        next_action = "The plan allows extra charging to catch up when enough power is available."
    elif assessment.recovery_state == "waiting_for_measurements":
        next_action = "Waiting for energy readings to confirm how much charging is still needed."
    elif assessment.reason in ("battery_full", "stored_target_reached"):
        next_action = "Keep this energy for the next planned use."
    if contract:
        objective = next((o for o in contract.objectives if o.kind == "stored_energy" and o.start_ms <= live.at_ms < o.deadline_ms), None)
        deadline = objective.deadline_ms if objective else None
    return {"status": status, "plan": plans[row.operation] if row else "Waiting for a current battery plan.",
            "now": now, "difference": difference, "next": next_action, "deadline_ms": deadline}
