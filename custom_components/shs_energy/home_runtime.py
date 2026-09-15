"""Pure, offline command reconciliation protocol; not connected to live controls."""
from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite
from typing import Literal, Optional, Union

if __package__:
    from .battery_execution_policy import (ExecutionPolicy, ExecutionConditions, ContextIdentity,
        Permissions, BatteryPlant, BatteryOperation, Decision, OutsideCoverage, evaluate_policy, NUMERICAL_TOLERANCE_SEK)
    from .battery_supply import SupplyScope
    from .runtime_json import read_runtime_json
    from .energy_ledger import (
        EnergyLedger, CounterSample, ActualsWatermark, StreamActuals, SettledActuals,
        actuals_since, record_actuals, prune_ledger, start_settlement,
        settle_and_prune, reconciled_actuals, validate_settlement,
    )
else:
    from battery_execution_policy import (ExecutionPolicy, ExecutionConditions, ContextIdentity,
        Permissions, BatteryPlant, BatteryOperation, Decision, OutsideCoverage, evaluate_policy, NUMERICAL_TOLERANCE_SEK)
    from battery_supply import SupplyScope
    from runtime_json import read_runtime_json
    from energy_ledger import (
        EnergyLedger, CounterSample, ActualsWatermark, StreamActuals, SettledActuals,
        actuals_since, record_actuals, prune_ledger, start_settlement,
        settle_and_prune, reconciled_actuals, validate_settlement,
    )

Value = Union[str, float, int]
Controls = tuple[tuple[str, Value], ...]
Measurements = tuple[tuple[str, float], ...]
Mode = Literal["monitoring", "planning", "control_verification", "controlling"]
Purpose = Literal["optimisation", "release"]


def _number(value, label, *, positive=False):
    if type(value) not in (float, int) or not isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f"{label} must be finite and {'positive' if positive else 'nonnegative'}")


def _integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 2 ** 53:
        raise ValueError("counter/time must be a bounded integer")


def _identity(value):
    if type(value) is not str or not 0 < len(value) <= 128:
        raise ValueError("identity must be a nonempty string of at most 128 characters")


def _pairs(values):
    if type(values) is not tuple or len(values) > 32 or len({v[0] for v in values}) != len(values):
        raise ValueError("at most 32 uniquely named values are supported")
    for key, value in values:
        _identity(key)
        if type(value) is str:
            _identity(value)
        elif type(value) not in (int, float) or not isfinite(value):
            raise ValueError("control/measurement values must be strings or finite numbers")


@dataclass(frozen=True)
class Envelope:
    import_w: float
    export_w: float

    def __post_init__(self):
        _number(self.import_w, "import envelope")
        _number(self.export_w, "export envelope")


@dataclass(frozen=True)
class Guard:
    key: str
    minimum: float
    maximum: float

    def __post_init__(self):
        _identity(self.key)
        if any(type(v) not in (int, float) or not isfinite(v) for v in (self.minimum, self.maximum)) or self.minimum > self.maximum:
            raise ValueError("native guard needs ordered finite bounds")


@dataclass(frozen=True)
class WriterIdentity:
    owner_id: str
    config_revision: str
    control_surface_revision: str

    def __post_init__(self):
        for value in (self.owner_id, self.config_revision, self.control_surface_revision):
            _identity(value)


@dataclass(frozen=True)
class WriterGrant:
    owner_id: str
    epoch: int
    config_revision: str
    control_surface_revision: str
    expires_at_ms: int

    def __post_init__(self):
        for value in (self.owner_id, self.config_revision, self.control_surface_revision):
            _identity(value)
        _integer(self.epoch, 1)
        _integer(self.expires_at_ms, 1)


@dataclass(frozen=True)
class ExternalDemand:
    """Real observed demand plus unresolved possible effects, never a planned action."""
    group_id: str
    observed: Envelope
    unresolved: Envelope

    def __post_init__(self):
        _identity(self.group_id)

    @property
    def possible(self):
        return Envelope(max(self.observed.import_w, self.unresolved.import_w),
                        max(self.observed.export_w, self.unresolved.export_w))


@dataclass(frozen=True)
class Request:
    id: str
    revision: int
    valid_until_ms: int
    target: Controls
    native_guards: tuple[Guard, ...]

    def __post_init__(self):
        _identity(self.id)
        _pairs(self.target)
        _integer(self.revision)
        _integer(self.valid_until_ms, 1)
        if self.revision < 0 or self.valid_until_ms <= 0 or len(self.native_guards) > 32:
            raise ValueError("invalid request bounds")


@dataclass(frozen=True)
class Step:
    key: str
    value: Value
    before: Controls
    native_guards: tuple[Guard, ...]
    possible: Envelope
    confirmation_timeout_ms: int
    latest_effect_delay_ms: int
    repeat_identical: bool
    evidence: str
    relief_id: Optional[str] = None

    def __post_init__(self):
        _pairs(self.before)
        _pairs(((self.key, self.value),))
        _identity(self.evidence)
        if self.relief_id is not None:
            _identity(self.relief_id)
        _integer(self.confirmation_timeout_ms, 1)
        _integer(self.latest_effect_delay_ms)
        if self.confirmation_timeout_ms <= 0 or self.latest_effect_delay_ms < 0 or len(self.native_guards) > 32:
            raise ValueError("invalid step timing or guards")
        if type(self.repeat_identical) is not bool:
            raise ValueError("repeatability must be explicit")

    @property
    def after(self):
        return tuple((key, self.value if key == self.key else value) for key, value in self.before)


@dataclass(frozen=True)
class ReliefRule:
    """Commissioned aggregate-only transition, never inferred from register order."""
    id: str
    before: Controls
    after: Controls
    observed: Envelope
    during: Envelope
    settled: Envelope
    native_guards: tuple[Guard, ...]
    evidence: str

    def __post_init__(self):
        _identity(self.id)
        _identity(self.evidence)
        _pairs(self.before)
        _pairs(self.after)
        if (set(dict(self.before)) != set(dict(self.after))
                or sum(dict(self.after)[k] != v for k, v in self.before) != 1
                or not _within(self.during, self.observed)
                or not _within(self.settled, self.during)
                or self.settled == self.observed or len(self.native_guards) > 32):
            raise ValueError("relief needs one assignment and non-worsening proven envelopes")


@dataclass(frozen=True)
class GroupSpec:
    id: str
    adapter_revision: str
    control_keys: tuple[str, ...]
    maximum: Envelope
    relief_rules: tuple[ReliefRule, ...] = ()
    writer: Optional[WriterIdentity] = None

    def __post_init__(self):
        _identity(self.id)
        _identity(self.adapter_revision)
        if not 0 < len(self.control_keys) <= 32 or len(set(self.control_keys)) != len(self.control_keys):
            raise ValueError("group needs unique bounded control keys")
        for key in self.control_keys:
            _identity(key)
        if len(self.relief_rules) > 32 or len({r.id for r in self.relief_rules}) != len(self.relief_rules):
            raise ValueError("relief rules need bounded unique identities")
        for rule in self.relief_rules:
            if set(dict(rule.before)) != set(self.control_keys) or not _within(rule.observed, self.maximum):
                raise ValueError("relief rule exceeds group scope")


@dataclass(frozen=True)
class Limits:
    dispatch_window_ms: int
    retry_base_ms: int
    retry_max_ms: int
    transition_timeout_ms: int = 1000

    def __post_init__(self):
        for value in (self.dispatch_window_ms, self.retry_base_ms, self.retry_max_ms, self.transition_timeout_ms):
            _integer(value, 1)
        if not 0 < self.dispatch_window_ms <= 60000 or not 0 < self.retry_base_ms <= self.retry_max_ms <= 3600000:
            raise ValueError("invalid dispatch/retry limits")
        if self.transition_timeout_ms > 60000:
            raise ValueError("transition timeout exceeds supported bound")


@dataclass(frozen=True)
class Observation:
    revision: int
    at_ms: int
    valid_until_ms: int
    controls: Controls
    measurements: Measurements
    envelope: Envelope

    def __post_init__(self):
        _pairs(self.controls)
        _pairs(self.measurements)
        for _, value in self.measurements:
            if type(value) not in (int, float):
                raise ValueError("native measurements must be numeric")
        for value in (self.revision, self.at_ms, self.valid_until_ms):
            _integer(value)
        if self.revision < 0 or not 0 <= self.at_ms < self.valid_until_ms:
            raise ValueError("observation needs valid revision and absolute freshness")


@dataclass(frozen=True)
class Frame:
    revision: int
    at_ms: int
    valid_until_ms: int
    external: Envelope
    limits: Envelope
    external_demands: tuple[ExternalDemand, ...] = ()

    def __post_init__(self):
        for value in (self.revision, self.at_ms, self.valid_until_ms):
            _integer(value)
        if self.revision < 0 or not 0 <= self.at_ms < self.valid_until_ms:
            raise ValueError("invalid physical frame")
        if len(self.external_demands) > 32 or len({e.group_id for e in self.external_demands}) != len(self.external_demands):
            raise ValueError("physical frame has duplicate/unbounded external evidence")


@dataclass(frozen=True)
class Plan:
    generation: int
    request_id: str
    request_revision: int
    purpose: Purpose
    steps: tuple[Step, ...]
    index: int = 0

    def __post_init__(self):
        _identity(self.request_id)
        for value in (self.generation, self.request_revision, self.index):
            _integer(value)
        if self.purpose not in ("optimisation", "release"):
            raise ValueError("invalid sequence purpose")
        if not 0 < len(self.steps) <= 8 or not 0 <= self.index <= len(self.steps):
            raise ValueError("invalid transition length/index")


@dataclass(frozen=True)
class Attempt:
    id: str
    prepared_revision: int
    generation: int
    request_id: str
    request_revision: int
    purpose: Purpose
    step_index: int
    step: Step
    observed_revision: int
    prepared_at_ms: int
    send_by_ms: int
    confirmation_deadline_ms: int
    latest_effect_ms: int
    stage: Literal["prepared", "sent", "accepted", "ambiguous"]
    grant: WriterGrant

    def __post_init__(self):
        _identity(self.id)
        _identity(self.request_id)
        for value in (self.prepared_revision, self.generation, self.request_revision, self.step_index,
                      self.observed_revision, self.prepared_at_ms, self.send_by_ms,
                      self.confirmation_deadline_ms, self.latest_effect_ms):
            _integer(value)
        if self.step_index >= 8 or self.purpose not in ("optimisation", "release") or self.stage not in ("prepared", "sent", "accepted", "ambiguous"):
            raise ValueError("invalid attempt identity/stage")
        if not 0 <= self.prepared_at_ms < self.send_by_ms <= self.latest_effect_ms or self.confirmation_deadline_ms < self.prepared_at_ms:
            raise ValueError("invalid attempt deadlines")


@dataclass(frozen=True)
class TransitionKey:
    generation: int
    request_id: str
    request_revision: int
    purpose: Purpose
    observed_revision: int

    def __post_init__(self):
        _identity(self.request_id)
        for value in (self.generation, self.request_revision, self.observed_revision):
            _integer(value)
        if self.purpose not in ("optimisation", "release"):
            raise ValueError("invalid transition purpose")


@dataclass(frozen=True)
class TransitionJob:
    key: TransitionKey
    token: int
    attempt: int
    deadline_ms: int

    def __post_init__(self):
        for value in (self.token, self.attempt, self.deadline_ms):
            _integer(value, 1)
        if self.attempt > 32:
            raise ValueError("invalid transition retry count")


@dataclass(frozen=True)
class TransitionFailure:
    key: TransitionKey
    attempt: int
    retry_at_ms: Optional[int]
    reason: str

    def __post_init__(self):
        _integer(self.attempt, 1)
        _identity(self.reason)
        if self.attempt > 32:
            raise ValueError("invalid transition retry count")
        if self.retry_at_ms is not None:
            _integer(self.retry_at_ms)


@dataclass(frozen=True)
class Group:
    spec: GroupSpec
    mode: Mode = "monitoring"
    mode_revision: int = 0
    authority_confirmed: bool = False
    grant: Optional[WriterGrant] = None
    grant_epoch: int = 0
    grant_confirmed: bool = False
    release_pending: bool = False
    desired: Optional[Request] = None
    release: Optional[Request] = None
    observation: Optional[Observation] = None
    observation_revision: int = -1
    generation: int = 0
    plan: Optional[Plan] = None
    attempts: tuple[Attempt, ...] = ()
    retry_not_before_ms: int = 0
    consecutive_attempts: int = 0
    next_attempt: int = 1
    owned: bool = False
    transition_work: Optional[Union[TransitionJob, TransitionFailure]] = None
    next_transition: int = 1
    status: str = "inactive"
    journal_fault: bool = False

    def __post_init__(self):
        for value in (self.mode_revision, self.generation, self.grant_epoch, self.retry_not_before_ms, self.consecutive_attempts):
            _integer(value)
        _integer(self.next_attempt, 1)
        _integer(self.next_transition, 1)
        _integer(self.observation_revision, -1)
        if self.mode not in ("monitoring", "planning", "control_verification", "controlling"):
            raise ValueError("invalid group mode")
        if len(self.attempts) > 64 or len({a.id for a in self.attempts}) != len(self.attempts):
            raise ValueError("unresolved attempt inventory exceeds its bound")
        if sum(a.stage == "prepared" for a in self.attempts) > 1:
            raise ValueError("only one preparation per group is allowed")
        if min(self.mode_revision, self.generation, self.retry_not_before_ms, self.consecutive_attempts) < 0 or self.next_attempt < 1:
            raise ValueError("invalid group counters")


@dataclass(frozen=True)
class OperationBinding:
    operation: BatteryOperation
    target: Controls
    response_model_revision: str
    response_evidence: str
    native_guards: tuple[Guard, ...] = ()

    def __post_init__(self):
        _pairs(self.target)
        _identity(self.response_evidence)
        if self.response_model_revision != "pv-first-v1":
            raise ValueError("unsupported local response model")


@dataclass(frozen=True)
class NativeCatalog:
    """Local exact native mapping. Synthetic evidence is not commissioning evidence."""
    revision: str
    adapter_revision: str
    control_surface_revision: str
    mode_key: str
    charge_key: str
    discharge_key: str
    mode_options: tuple[str, ...]
    quantum_w: float
    charge_max_w: float
    discharge_max_w: float
    bindings: tuple[OperationBinding, ...]

    def __post_init__(self):
        for value in (self.revision, self.adapter_revision, self.control_surface_revision,
                      self.mode_key, self.charge_key, self.discharge_key):
            _identity(value)
        _number(self.quantum_w, "native quantum", positive=True)
        _number(self.charge_max_w, "charge capability")
        _number(self.discharge_max_w, "discharge capability")
        keys = {self.mode_key, self.charge_key, self.discharge_key}
        if len(keys) != 3 or not 0 < len(self.bindings) <= 12 or len({b.operation.id for b in self.bindings}) != len(self.bindings):
            raise ValueError("catalog needs unique surfaces and operation bindings")
        modes = {"self_consumption": "Maximum Self Consumption", "solar_charge": "Maximum Self Consumption",
                 "supply_house": "Maximum Self Consumption", "grid_charge": "Command Charging (PV First)",
                 "export": "Command Discharging (PV First)", "hold": "Standby"}
        for binding in self.bindings:
            op, target = binding.operation, dict(binding.target)
            if op.operation not in modes or set(target) != keys or target[self.mode_key] != modes[op.operation] or target[self.mode_key] not in self.mode_options:
                raise ValueError("unsupported native operation mapping")
            cc, dc = op.charge_limit_w, op.discharge_limit_w
            expected = (self.charge_max_w, self.discharge_max_w) if op.operation == "self_consumption" else (cc, dc)
            if op.operation == "self_consumption" and (cc, dc) != expected:
                raise ValueError("self consumption must score the actual rated ceilings")
            if op.operation == "solar_charge" and cc <= 0 or op.operation == "supply_house" and dc <= 0:
                raise ValueError("automatic directional operation needs a ceiling")
            if ((op.operation in ("solar_charge", "grid_charge") and dc != 0)
                    or (op.operation in ("supply_house", "export") and cc != 0)
                    or (op.operation == "hold" and (cc != 0 or dc != 0))):
                raise ValueError("native source semantics differ")
            for key, power, maximum in zip((self.charge_key, self.discharge_key), expected,
                                            (self.charge_max_w, self.discharge_max_w)):
                _number(target[key], "native ceiling")
                if target[key] != power or power > maximum or abs(power / self.quantum_w - round(power / self.quantum_w)) > 1e-9:
                    raise ValueError("native target differs from exact quantum/capability")


@dataclass(frozen=True)
class ScopeParticipant:
    group_id: str
    mode: Mode
    mode_revision: int
    owner: Literal["new_runtime", "legacy", "external", "none"]
    control_surface_ids: tuple[str, ...]

    def __post_init__(self):
        _identity(self.group_id)
        _integer(self.mode_revision)
        if self.mode not in ("monitoring", "planning", "control_verification", "controlling") or self.owner not in ("new_runtime", "legacy", "external", "none"):
            raise ValueError("invalid scope owner/mode")
        if not self.control_surface_ids or len(self.control_surface_ids) > 32:
            raise ValueError("scope needs bounded physical surfaces")
        for surface in self.control_surface_ids:
            _identity(surface)


@dataclass(frozen=True)
class ExecutionScope:
    revision: str
    battery_group_id: str
    participants: tuple[ScopeParticipant, ...]

    def __post_init__(self):
        _identity(self.revision)
        _identity(self.battery_group_id)
        ids = [p.group_id for p in self.participants]
        surfaces = [s for p in self.participants for s in p.control_surface_ids]
        if not 0 < len(ids) <= 32 or len(set(ids)) != len(ids) or self.battery_group_id not in ids or len(surfaces) != len(set(surfaces)):
            raise ValueError("overlapping or incomplete execution scope")
        battery = next(p for p in self.participants if p.group_id == self.battery_group_id)
        if battery.owner != "new_runtime":
            raise ValueError("policy battery must have the runtime request owner")


@dataclass(frozen=True)
class ExecutionAuthority:
    """Install configured identities, native catalog and mixed scope atomically."""
    config_revision: str
    identity: ContextIdentity
    permissions: Permissions
    plant: BatteryPlant
    scope: ExecutionScope
    catalog: NativeCatalog
    owner_id: str
    supply_scope: SupplyScope

    def __post_init__(self):
        if not isinstance(self.supply_scope, SupplyScope):
            raise ValueError("configured battery supply scope is required")
        _identity(self.config_revision)
        _identity(self.owner_id)
        if (self.identity.battery_id != self.scope.battery_group_id
                or self.identity.scope_revision != self.scope.revision
                or self.identity.catalog_revision != self.catalog.revision
                or self.identity.response_model_revision != "pv-first-v1"
                or self.catalog.charge_max_w != self.plant.charge_max_w
                or self.catalog.discharge_max_w != self.plant.discharge_max_w):
            raise ValueError("configured scope/catalog/plant identity mismatch")


@dataclass(frozen=True)
class PolicySession:
    compiled: ExecutionPolicy
    watermark: ActualsWatermark
    settled_actuals: tuple[SettledActuals, ...]
    reconciled_from: Optional[ActualsWatermark] = None
    reconciled_actuals: tuple[StreamActuals, ...] = ()
    decision: Optional[Decision] = None
    status: Literal["active", "diagnostic_only", "outside_coverage", "awaiting_context"] = "awaiting_context"
    request_id: Optional[str] = None
    selected_id: Optional[str] = None
    candidate_id: Optional[str] = None
    candidate_since_ms: int = 0
    candidate_observations: int = 0
    candidate_observation_revision: int = -1
    refresh_requested: bool = False

    def __post_init__(self):
        for value in (self.request_id, self.selected_id, self.candidate_id):
            if value is not None:
                _identity(value)
        _integer(self.candidate_since_ms)
        _integer(self.candidate_observation_revision, -1)
        _integer(self.candidate_observations)
        if self.candidate_observations > 2 or (self.candidate_id is None and self.candidate_observations):
            raise ValueError("invalid hysteresis observation count")

    @property
    def revision(self):
        return self.compiled.summary.identity.revision


@dataclass(frozen=True)
class HomeState:
    revision: int
    last_time_ms: int
    limits: Limits
    groups: tuple[Group, ...]
    frame: Optional[Frame] = None
    resume_after_ms: int = 0
    ledger: Optional[EnergyLedger] = None
    authority: Optional[ExecutionAuthority] = None
    authority_revision: int = -1
    conditions: Optional[ExecutionConditions] = None
    conditions_revision: int = -1
    policy: Optional[PolicySession] = None

    def __post_init__(self):
        for value in (self.revision, self.last_time_ms, self.resume_after_ms):
            _integer(value)
        _integer(self.authority_revision, -1)
        _integer(self.conditions_revision, -1)
        if not 0 < len(self.groups) <= 32 or len({g.spec.id for g in self.groups}) != len(self.groups):
            raise ValueError("home needs 1..32 unique groups")
        if self.revision < 0 or self.last_time_ms < 0:
            raise ValueError("invalid home revision/time")


# Events are immutable evidence, not executable callbacks.
@dataclass(frozen=True)
class AuthorityInstalled:
    authority: ExecutionAuthority
    revision: int


@dataclass(frozen=True)
class ConditionsObserved:
    conditions: ExecutionConditions


@dataclass(frozen=True)
class PolicyOffered:
    compiled: ExecutionPolicy
    watermark: ActualsWatermark


@dataclass(frozen=True)
class GrantConfirmed:
    group_id: str
    grant: WriterGrant


@dataclass(frozen=True)
class GrantRevoked:
    group_id: str
    epoch: int


@dataclass(frozen=True)
class ReleaseApproved:
    group_id: str
    request: Request


@dataclass(frozen=True)
class MeterObserved:
    sample: CounterSample


@dataclass(frozen=True)
class LedgerPruned:
    before_ms: int


@dataclass(frozen=True)
class Observed:
    group_id: str
    observation: Observation


@dataclass(frozen=True)
class FrameObserved:
    frame: Frame


@dataclass(frozen=True)
class AuthorityChanged:
    group_id: str
    mode: Mode
    revision: int
    approved_release: Optional[Request]


@dataclass(frozen=True)
class Requested:
    group_id: str
    mode_revision: int
    request: Request


@dataclass(frozen=True)
class Proposed:
    group_id: str
    generation: int
    request_id: str
    request_revision: int
    observed_revision: int
    adapter_revision: str
    steps: tuple[Step, ...]
    token: int


@dataclass(frozen=True)
class TransitionFailed:
    group_id: str
    token: int
    outcome: Literal["retryable", "unsupported"]
    reason: str


@dataclass(frozen=True)
class JournalDurable:
    revision: int


@dataclass(frozen=True)
class JournalFailed:
    revision: int


@dataclass(frozen=True)
class TransportResult:
    group_id: str
    attempt_id: str
    outcome: Literal["not_sent", "accepted", "ambiguous"]
    evidence: str


@dataclass(frozen=True)
class Tick:
    pass


Event = Union[AuthorityInstalled, ConditionsObserved, GrantConfirmed, GrantRevoked, ReleaseApproved, PolicyOffered, MeterObserved, LedgerPruned, Observed, FrameObserved, AuthorityChanged, Requested, Proposed, TransitionFailed, JournalDurable, JournalFailed, TransportResult, Tick]


@dataclass(frozen=True)
class Persist:
    state: HomeState


@dataclass(frozen=True)
class Send:
    group_id: str
    attempt_id: str
    key: str
    value: Value
    send_by_ms: int
    grant: WriterGrant
    generation: int
    request_id: str
    request_revision: int


@dataclass(frozen=True)
class NeedPolicy:
    reason: str


@dataclass(frozen=True)
class Observe:
    group_id: str


@dataclass(frozen=True)
class ConfirmAuthority:
    group_id: str


@dataclass(frozen=True)
class NeedTransition:
    group_id: str
    generation: int
    purpose: Purpose
    request: Request
    observation: Observation
    adapter_revision: str
    token: int
    deadline_ms: int


@dataclass(frozen=True)
class WakeAt:
    at_ms: int


@dataclass(frozen=True)
class Report:
    group_id: str
    reason: str


Effect = Union[NeedPolicy, Persist, Send, Observe, ConfirmAuthority, NeedTransition, WakeAt, Report]


def create_home(specs: tuple[GroupSpec, ...], limits: Limits, *, ledger: Optional[EnergyLedger] = None) -> HomeState:
    return HomeState(0, 0, limits, tuple(Group(spec) for spec in specs), ledger=ledger)


def _fresh(observation, now):
    return observation is not None and observation.at_ms <= now < observation.valid_until_ms


def _guards(guards, observation):
    measurements = dict(observation.measurements)
    return all(g.key in measurements and g.minimum <= measurements[g.key] <= g.maximum for g in guards)


def _same(left, right):
    return dict(left) == dict(right)


def _put(state, group):
    return replace(state, groups=tuple(group if g.spec.id == group.spec.id else g for g in state.groups))


def _request(group, now):
    if not group.authority_confirmed:
        return None, None
    if not group.release_pending and group.mode == "controlling" and group.desired is not None and now < group.desired.valid_until_ms:
        return group.desired, "optimisation"
    if group.release_pending or group.owned or group.attempts:
        if group.release is not None and now < group.release.valid_until_ms:
            return group.release, "release"
    return None, None


def _supersede(group):
    # In-process preparations have not been dispatched; sent/ambiguous attempts stay.
    retry = group.transition_work if isinstance(group.transition_work, TransitionFailure) and group.transition_work.retry_at_ms is not None else None
    return replace(group, generation=group.generation + 1, plan=None, transition_work=retry,
                   attempts=tuple(a for a in group.attempts if a.stage != "prepared"))


def reservation(group: Group, now_ms: int) -> Envelope:
    """Union within a group; independent group reservations are summed by the owner."""
    current = group.observation.envelope if _fresh(group.observation, now_ms) else group.spec.maximum
    envelopes = [current, *(a.step.possible for a in group.attempts)]
    return Envelope(max(e.import_w for e in envelopes), max(e.export_w for e in envelopes))


def _within(candidate, maximum):
    return candidate.import_w <= maximum.import_w and candidate.export_w <= maximum.export_w


def _room(state, group, extra, now):
    if not _scope_frame_valid(state, now):
        return False
    imported, exported = state.frame.external.import_w, state.frame.external.export_w
    for item in state.groups:
        if not _managed(state, item):
            continue
        bound = reservation(item, now)
        if item.spec.id == group.spec.id:
            bound = Envelope(max(bound.import_w, extra.import_w), max(bound.export_w, extra.export_w))
        imported += bound.import_w
        exported += bound.export_w
    return imported <= state.frame.limits.import_w and exported <= state.frame.limits.export_w


def _compatible(attempt, step):
    return (attempt.step.repeat_identical and step.repeat_identical and attempt.step.key == step.key
            and attempt.step.value == step.value and _same(attempt.step.after, step.after)
            and attempt.step.native_guards == step.native_guards and attempt.step.evidence == step.evidence
            and attempt.step.possible == step.possible and attempt.step.relief_id == step.relief_id)


def preparation_matches(plan, attempt):
    """The sole structural invariant for an unsent sequence preparation."""
    return (plan is not None and attempt.generation == plan.generation
            and attempt.step_index == plan.index and plan.index < len(plan.steps)
            and attempt.step == plan.steps[plan.index]
            and (attempt.request_id, attempt.request_revision, attempt.purpose) ==
                (plan.request_id, plan.request_revision, plan.purpose))


def _settle(group, now):
    observation = group.observation
    remaining = []
    plan = group.plan
    for attempt in group.attempts:
        # A fresh sample after the adapter's last possible effect settles old effects,
        # whether the setting matches current intent or represents external drift.
        settled = (attempt.stage != "prepared" and _fresh(observation, now)
                   and observation.at_ms >= attempt.latest_effect_ms
                   and observation.revision > attempt.observed_revision)
        if settled:
            if plan is not None and attempt.generation == plan.generation and attempt.step_index == plan.index:
                if _same(observation.controls, attempt.step.after) and _guards(attempt.step.native_guards, observation):
                    plan = replace(plan, index=plan.index + 1)
                else:
                    plan = None
        else:
            if attempt.stage in ("sent", "accepted") and now >= attempt.confirmation_deadline_ms:
                attempt = replace(attempt, stage="ambiguous")
            remaining.append(attempt)
    # A retry is unsent software work. Advancing or invalidating its sequence
    # must cancel it in the same state update; issued effects remain independent.
    remaining = tuple(a for a in remaining if a.stage != "prepared" or preparation_matches(plan, a))
    return replace(group, attempts=remaining, plan=plan)


def _relief_rule(group, step):
    if step.relief_id is None:
        return None
    rule = next((r for r in group.spec.relief_rules if r.id == step.relief_id), None)
    if (rule is None or not _same(rule.before, step.before) or not _same(rule.after, step.after)
            or rule.during != step.possible or rule.native_guards != step.native_guards
            or rule.evidence != step.evidence):
        raise ValueError("step does not match its commissioned relief rule")
    return rule


def _admissible(state, group, step, now):
    if _room(state, group, step.possible, now):
        return True
    rule = _relief_rule(group, step)
    if (rule is None or not _fresh(state.frame, now) or not _fresh(group.observation, now)
            or any(a.stage != "prepared" for a in group.attempts)
            or not _same(group.observation.controls, rule.before)
            or group.observation.envelope != rule.observed
            or not _guards(rule.native_guards, group.observation)):
        return False
    # The configured proof covers the complete transient; no generic register
    # decrease or unconfirmed supply earns headroom. Freshness is never bypassed.
    imported = state.frame.external.import_w + sum(reservation(g, now).import_w for g in state.groups if _managed(state, g))
    exported = state.frame.external.export_w + sum(reservation(g, now).export_w for g in state.groups if _managed(state, g))
    return ((imported > state.frame.limits.import_w and rule.settled.import_w < rule.observed.import_w)
            or (exported > state.frame.limits.export_w and rule.settled.export_w < rule.observed.export_w))


def _validate_target(group, request):
    if set(dict(request.target)) != set(group.spec.control_keys):
        raise ValueError("request must name the complete supported control surface")


def _validate_proposal(group, event, request, purpose):
    if not 0 < len(event.steps) <= 8:
        raise ValueError("proposal must have 1..8 steps")
    previous = group.observation.controls
    for step in event.steps:
        if step.key not in group.spec.control_keys or not _same(step.before, previous):
            raise ValueError("step guards must describe the complete preceding control surface")
        if not _within(step.possible, group.spec.maximum):
            raise ValueError("step effects exceed the declared group maximum")
        _relief_rule(group, step)
        previous = step.after
    if not _same(previous, request.target):
        raise ValueError("transition does not reach the current request")
    return Plan(group.generation, request.id, request.revision, purpose, event.steps)



def _transition_key(group, request, purpose):
    return TransitionKey(group.generation, request.id, request.revision, purpose, group.observation.revision)


def _transition_failure(job, now, limits, reason, *, unsupported=False):
    delay = min(limits.retry_max_ms, limits.retry_base_ms * 2 ** min(job.attempt - 1, 20))
    return TransitionFailure(job.key, job.attempt, None if unsupported else now + delay, reason)


def resumed_transition_work(group, now, limits):
    work = group.transition_work
    if isinstance(work, TransitionJob):
        return _transition_failure(work, max(now, work.deadline_ms), limits, "transition_interrupted")
    return work


def _need_transition(group, request, purpose, now, limits, effects):
    key = _transition_key(group, request, purpose)
    work = group.transition_work
    if isinstance(work, TransitionJob) and work.key == key:
        if now < work.deadline_ms:
            return replace(group, status="needs_transition")
        work = _transition_failure(work, work.deadline_ms, limits, "transition_timeout")
        effects.append(Report(group.spec.id, "transition_timeout"))
    if isinstance(work, TransitionFailure):
        if work.retry_at_ms is None and work.key == key:
            return replace(group, transition_work=work, status="transition_unsupported")
        if work.retry_at_ms is not None and now < work.retry_at_ms:
            return replace(group, transition_work=work, status="transition_retry_wait")
    attempt = min(work.attempt + 1, 32) if isinstance(work, TransitionFailure) else 1
    job = TransitionJob(key, group.next_transition, attempt, min(now + limits.transition_timeout_ms,
                        request.valid_until_ms, group.observation.valid_until_ms))
    effects.append(NeedTransition(group.spec.id, group.generation, purpose, request,
                                 group.observation, group.spec.adapter_revision, job.token, job.deadline_ms))
    return replace(group, transition_work=job, next_transition=group.next_transition + 1,
                   status="needs_transition")


def _battery_group(state):
    return None if state.authority is None else next(g for g in state.groups if g.spec.id == state.authority.scope.battery_group_id)


def _is_battery(state, group):
    return state.authority is not None and group.spec.id == state.authority.scope.battery_group_id


def _managed(state, group):
    if state.authority is None:
        return True
    participant = next(p for p in state.authority.scope.participants if p.group_id == group.spec.id)
    return participant.owner == "new_runtime"


def _scope_frame_valid(state, now):
    if not _fresh(state.frame, now):
        return False
    if state.authority is None:
        return True
    external = {p.group_id for p in state.authority.scope.participants if p.owner != "new_runtime"}
    evidence = state.frame.external_demands
    by_id = {e.group_id: e for e in evidence}
    # A scope handover changes who supplies demand, not whether an issued effect
    # can still arrive. The external envelope must include our retained effects
    # before _room may count that participant solely through Frame.external.
    for group in state.groups:
        if group.spec.id in external and any(
                group.spec.id not in by_id or not _within(a.step.possible, by_id[group.spec.id].unresolved)
                for a in group.attempts):
            return False
    return (len(evidence) == len(external) and {e.group_id for e in evidence} == external
            and sum(e.possible.import_w for e in evidence) <= state.frame.external.import_w
            and sum(e.possible.export_w for e in evidence) <= state.frame.external.export_w)


def _grant_valid(state, group, now):
    grant = group.grant
    if (not group.grant_confirmed or grant is None or grant.epoch != group.grant_epoch
            or now >= grant.expires_at_ms or not _managed(state, group)):
        return False
    expected = (WriterIdentity(state.authority.owner_id, state.authority.config_revision,
                               state.authority.catalog.control_surface_revision)
                if _is_battery(state, group) else group.spec.writer)
    if expected is None or (grant.owner_id, grant.config_revision, grant.control_surface_revision) != (
            expected.owner_id, expected.config_revision, expected.control_surface_revision):
        return False
    return True


def _validate_authority(state, authority):
    participants = {p.group_id: p for p in authority.scope.participants}
    if not {g.spec.id for g in state.groups} <= participants.keys():
        raise ValueError("execution scope omits runtime groups")
    if any(set(participants[g.spec.id].control_surface_ids) != set(g.spec.control_keys) for g in state.groups):
        raise ValueError("scope differs from actual configured control surfaces")
    group = next((g for g in state.groups if g.spec.id == authority.scope.battery_group_id), None)
    if (group is None or group.spec.adapter_revision != authority.catalog.adapter_revision
            or set(group.spec.control_keys) != {authority.catalog.mode_key, authority.catalog.charge_key, authority.catalog.discharge_key}
            or set(participants[group.spec.id].control_surface_ids) != set(group.spec.control_keys)):
        raise ValueError("catalog differs from local group surfaces/adapter")


def _validate_policy(state, compiled):
    authority, summary = state.authority, compiled.summary
    if authority is None or summary.identity.context != authority.identity or summary.permissions != authority.permissions or summary.plant != authority.plant or summary.supply_scope != authority.supply_scope:
        raise ValueError("policy differs from configured authority")
    participants = {p.group_id: p for p in authority.scope.participants}
    if any((g.mode, g.mode_revision) != (participants[g.spec.id].mode, participants[g.spec.id].mode_revision)
           for g in state.groups):
        raise ValueError("policy scope differs from requested operating modes")
    bindings = {b.operation.id: b for b in authority.catalog.bindings}
    if any(op.id not in bindings or bindings[op.id].operation != op for op in summary.operations):
        raise ValueError("policy operation lacks an exact local native binding")


def _accept_policy(state, event, now):
    summary = event.compiled.summary
    if state.policy and summary.identity.revision <= state.policy.revision:
        raise ValueError("duplicate or stale policy revision")
    _validate_policy(state, event.compiled)
    if state.ledger is None or event.watermark.at_ms != summary.actuals_origin_ms or not summary.from_ms <= now < summary.until_ms:
        raise ValueError("policy needs a real source watermark and current validity")
    if event.watermark.at_ms > now:
        raise ValueError("future policy source cut")
    # Admission is atomic: validate both the retained tail and the old settled
    # prefix before installing anything. Never reconcile through receipt time.
    actuals_since(state.ledger, event.watermark, now)
    old = state.policy
    reconciliation = reconciled_actuals(state.ledger, old.watermark, old.settled_actuals,
                                        event.watermark.at_ms) if old else ()
    session = PolicySession(event.compiled, event.watermark, start_settlement(state.ledger, event.watermark),
                            old.watermark if old else None, reconciliation,
                            request_id=old.request_id if old else None,
                            selected_id=old.selected_id if old else None,
                            candidate_id=old.candidate_id if old else None,
                            candidate_since_ms=old.candidate_since_ms if old else 0,
                            candidate_observations=old.candidate_observations if old else 0,
                            candidate_observation_revision=old.candidate_observation_revision if old else -1)
    return replace(state, policy=session)


def validate_policy_settlement(state):
    validate_settlement(state.ledger, state.policy.watermark, state.policy.settled_actuals)
    if any(prefix.through_ms > state.last_time_ms for prefix in state.policy.settled_actuals):
        raise ValueError("settlement exceeds checkpoint time")


def _withdraw_policy(state, reason, effects, *, refresh=True):
    session, group = state.policy, _battery_group(state)
    if group.desired is not None:
        group = replace(_supersede(group), desired=None,
                        release_pending=group.release_pending or group.owned or any(a.stage != "prepared" for a in group.attempts))
        state = _put(state, group)
    if refresh and not session.refresh_requested:
        effects.append(NeedPolicy(reason))
    return replace(state, policy=replace(session, status="outside_coverage", decision=None,
                    candidate_id=None, candidate_since_ms=0, candidate_observations=0,
                    candidate_observation_revision=-1,
                    refresh_requested=session.refresh_requested or refresh))


def _refresh_policy_decision(state, now, effects):
    session = state.policy
    if session is None:
        return state
    group, conditions = _battery_group(state), state.conditions
    summary = session.compiled.summary
    if not summary.from_ms <= now < summary.until_ms:
        return _withdraw_policy(state, "policy_expired", effects)
    try:
        _validate_policy(state, session.compiled)
    except ValueError:
        return _withdraw_policy(state, "policy_authority_changed", effects)
    if conditions is None:
        return replace(state, policy=replace(session, status="awaiting_context", decision=None))
    if not _fresh(conditions, now):
        effects.append(Observe(group.spec.id))
        return _withdraw_policy(state, "stale_conditions", effects, refresh=False)
    if conditions.identity != state.authority.identity or conditions.permissions != state.authority.permissions:
        return _withdraw_policy(state, "conditions_authority_mismatch", effects)
    if state.frame and (conditions.residual_load_w < sum(e.observed.import_w for e in state.frame.external_demands)):
        return _withdraw_policy(state, "external_demand_missing", effects)
    decision = evaluate_policy(session.compiled, conditions, now, incumbent_id=session.selected_id, deadband_sek=.02)
    if isinstance(decision, OutsideCoverage):
        return _withdraw_policy(state, decision.reason, effects, refresh=decision.refresh_required)
    ranked = {r.operation.id: r for r in decision.ranked}
    # Local physical and native evidence can make an otherwise economic option
    # ineligible. Such changes bypass discretionary sustain immediately.
    if _scope_frame_valid(state, now) and _fresh(group.observation, now):
        eligible = {}
        for key, row in ranked.items():
            local = next(b for b in state.authority.catalog.bindings if b.operation.id == key)
            predicted = Envelope(row.possible_import_w, row.possible_export_w)
            drift = (row.operation.operation == "export" and _same(group.observation.controls, local.target)
                     and not _within(group.observation.envelope, predicted))
            if _room(state, group, predicted, now) and _guards(local.native_guards, group.observation) and not drift:
                eligible[key] = row
        ranked = eligible
        if not ranked:
            return _withdraw_policy(state, "physical_scope_uncovered", effects)
    minimum = min(row.total_delta_sek for row in ranked.values())
    best = min((row for row in ranked.values() if row.total_delta_sek <= minimum + NUMERICAL_TOLERANCE_SEK),
               key=lambda row: row.operation.id)
    incumbent = ranked.get(session.selected_id)
    selected = incumbent.operation.id if incumbent and incumbent.total_delta_sek <= best.total_delta_sek + .02 else best.operation.id
    decision = replace(decision, selected_id=selected)
    binding = next(b for b in state.authority.catalog.bindings if b.operation.id == selected)
    same_target = group.desired is not None and _same(group.desired.target, binding.target) and group.desired.native_guards == binding.native_guards
    if selected != session.selected_id and session.selected_id in ranked and not same_target:
        if selected != session.candidate_id:
            session = replace(session, candidate_id=selected, candidate_since_ms=now,
                              candidate_observations=1, candidate_observation_revision=conditions.revision)
        elif conditions.revision > session.candidate_observation_revision and session.candidate_observations < 2:
            session = replace(session, candidate_observations=min(2, session.candidate_observations + 1),
                              candidate_observation_revision=conditions.revision)
        if now - session.candidate_since_ms < 5000 or session.candidate_observations < 2:
            selected = session.selected_id
            binding = next(b for b in state.authority.catalog.bindings if b.operation.id == selected)
    if selected == decision.selected_id:
        session = replace(session, candidate_id=None, candidate_since_ms=0,
                          candidate_observations=0, candidate_observation_revision=-1)
    if decision.refresh_due and not session.refresh_requested:
        effects.append(NeedPolicy("refresh_due"))
        session = replace(session, refresh_requested=True)
    session = replace(session, decision=replace(decision, selected_id=selected), selected_id=selected,
                      status="active" if group.mode == "controlling" else "diagnostic_only")
    if group.mode == "controlling":
        # A fresh scope/policy may supersede an unissued release on rapid entry.
        # Issued release effects still reconcile in _drive before incompatible work.
        if group.release_pending:
            group = replace(_supersede(group), release_pending=False)
        # Request identity belongs to executable intent, not economic policy revision.
        if group.desired and _same(group.desired.target, binding.target) and group.desired.native_guards == binding.native_guards:
            request = replace(group.desired, valid_until_ms=min(summary.until_ms, decision.valid_until_ms))
        else:
            group = _supersede(group)
            request = Request(f"battery:{group.generation}", group.generation,
                              min(summary.until_ms, decision.valid_until_ms), binding.target, binding.native_guards)
        group = replace(group, desired=request)
        session = replace(session, request_id=request.id)
    elif group.desired is not None:
        group = replace(_supersede(group), desired=None,
                        release_pending=group.release_pending or group.owned or bool(group.attempts))
    return replace(_put(state, group), policy=session)


def _policy_send_valid(state, group, request, now):
    if not _is_battery(state, group):
        return True
    session = state.policy
    if session is None or session.status != "active" or not _fresh(state.conditions, now):
        return False
    try:
        _validate_policy(state, session.compiled)
        decision = evaluate_policy(session.compiled, state.conditions, now,
                                   incumbent_id=session.selected_id, deadband_sek=.02)
    except ValueError:
        return False
    if isinstance(decision, OutsideCoverage) or state.conditions.identity != state.authority.identity or state.conditions.permissions != state.authority.permissions:
        return False
    chosen = next((r for r in decision.ranked if r.operation.id == session.selected_id), None)
    binding = next((b for b in state.authority.catalog.bindings if b.operation.id == session.selected_id), None)
    return (chosen is not None and binding is not None and _same(request.target, binding.target)
            and request.native_guards == binding.native_guards
            and state.conditions.residual_load_w >= sum(e.observed.import_w for e in state.frame.external_demands)
            and _room(state, group, Envelope(chosen.possible_import_w, chosen.possible_export_w), now))


def authorize_send(state: HomeState, send: Send, now_ms: int) -> bool:
    """Pure final-port guard. Host must also compare with the live grant arbiter."""
    if type(now_ms) is not int or now_ms < max(state.last_time_ms, state.resume_after_ms):
        return False
    group = next((g for g in state.groups if g.spec.id == send.group_id), None)
    if group is None or not _grant_valid(state, group, now_ms):
        return False
    attempt = next((a for a in group.attempts if a.id == send.attempt_id), None)
    request, purpose = _request(group, now_ms)
    return (attempt is not None and attempt.stage == "sent" and request is not None
            and send.grant == group.grant == attempt.grant
            and send.generation == group.generation == attempt.generation
            and (send.request_id, send.request_revision) == (request.id, request.revision) == (attempt.request_id, attempt.request_revision)
            and purpose == attempt.purpose and send.send_by_ms == attempt.send_by_ms
            and attempt.prepared_at_ms <= now_ms < attempt.send_by_ms
            and (send.key, send.value) == (attempt.step.key, attempt.step.value)
            and _fresh(group.observation, now_ms) and _scope_frame_valid(state, now_ms)
            and _same(group.observation.controls, attempt.step.before)
            and _guards(request.native_guards + attempt.step.native_guards, group.observation)
            and _admissible(_put(state, replace(group, attempts=tuple(a for a in group.attempts if a.id != attempt.id))),
                            replace(group, attempts=tuple(a for a in group.attempts if a.id != attempt.id)), attempt.step, now_ms)
            and (purpose == "release" or _policy_send_valid(state, group, request, now_ms)))


def _durable_view(state):
    """Frequent physical evidence and derived diagnostics do not journal by themselves."""
    policy = replace(state.policy, decision=None, status="awaiting_context") if state.policy else None
    return replace(state, last_time_ms=0, frame=None, conditions=None, conditions_revision=-1, policy=policy,
                   groups=tuple(replace(g, observation=None, observation_revision=-1, status="inactive") for g in state.groups))


def _drive(state, now, durable_revision, effects):
    for original in state.groups:
        group = _settle(original, now)
        request, purpose = _request(group, now)
        if not _grant_valid(state, group, now):
            if group.plan is not None or any(a.stage == "prepared" for a in group.attempts):
                group = _supersede(group)
            effects.append(ConfirmAuthority(group.spec.id))
            state = _put(state, replace(group, status="awaiting_grant" if request or group.owned else "inactive"))
            continue
        if purpose == "optimisation" and _is_battery(state, group) and (state.policy is None or state.policy.status != "active"):
            state = _put(state, replace(group, status="awaiting_context"))
            continue
        if not group.authority_confirmed:
            effects.append(ConfirmAuthority(group.spec.id))
        if request is None:
            group = replace(_supersede(group), status="release_required" if group.owned else "inactive") if group.plan else replace(group, status="release_required" if group.owned else "inactive")
            if group.attempts or group.owned:
                effects.append(Observe(group.spec.id))
            state = _put(state, group)
            continue
        if group.plan and (group.plan.generation != group.generation or group.plan.request_id != request.id
                           or group.plan.request_revision != request.revision or group.plan.purpose != purpose):
            group = _supersede(group)
        if not _fresh(group.observation, now) or not _scope_frame_valid(state, now):
            group = replace(group, status="awaiting_observation")
            effects.append(Observe(group.spec.id))
            state = _put(state, group)
            continue
        if not _guards(request.native_guards, group.observation):
            group = replace(group, plan=None, transition_work=None,
                            attempts=tuple(a for a in group.attempts if a.stage != "prepared"),
                            status="native_guard_blocked")
            effects.append(Observe(group.spec.id))
            state = _put(state, group)
            continue
        if _same(group.observation.controls, request.target):
            # Prepared work can be cancelled in-process. Issued work cannot be erased
            # just because the desired setting currently happens to be visible.
            group = replace(group, plan=None, transition_work=None,
                            attempts=tuple(a for a in group.attempts if a.stage != "prepared"))
            if group.attempts:
                group = replace(group, status="reconciling")
                effects.append(Observe(group.spec.id))
            else:
                group = replace(group, status="adopted" if purpose == "optimisation" else "released",
                                owned=purpose == "optimisation", release_pending=False if purpose == "release" else group.release_pending, consecutive_attempts=0)
            state = _put(state, group)
            continue
        if group.plan and group.plan.index == len(group.plan.steps):
            group = replace(group, plan=None, transition_work=None)
        prepared = next((a for a in group.attempts if a.stage == "prepared"), None)
        if prepared is not None:
            step = prepared.step
            valid = (prepared.generation == group.generation and prepared.grant == group.grant and now < prepared.send_by_ms
                     and (purpose == "release" or _policy_send_valid(state, group, request, now))
                     and _same(group.observation.controls, step.before) and _guards(step.native_guards, group.observation)
                     and _admissible(_put(state, group), group, step, now))
            if not valid:
                group = replace(group, attempts=tuple(a for a in group.attempts if a.id != prepared.id), plan=None, transition_work=None, status="reconciling")
                group = _need_transition(group, request, purpose, now, state.limits, effects)
            elif durable_revision == prepared.prepared_revision:
                attempt = replace(prepared, stage="sent", confirmation_deadline_ms=now + step.confirmation_timeout_ms)
                group = replace(group, attempts=tuple(attempt if a.id == prepared.id else a for a in group.attempts), owned=True, status="executing", journal_fault=False)
                effects.append(Send(group.spec.id, attempt.id, step.key, step.value, attempt.send_by_ms,
                                    attempt.grant, attempt.generation, attempt.request_id, attempt.request_revision))
            else:
                group = replace(group, status="journal_fault" if group.journal_fault else "awaiting_durability")
            state = _put(state, group)
            continue
        if now < group.retry_not_before_ms:
            group = replace(group, status="retry_wait")
            state = _put(state, group)
            continue
        if group.plan is None:
            group = _need_transition(group, request, purpose, now, state.limits, effects)
            state = _put(state, group)
            continue
        step = group.plan.steps[group.plan.index]
        if not _same(group.observation.controls, step.before) or not _guards(step.native_guards, group.observation):
            group = replace(group, plan=None, transition_work=None, status="reconciling")
            group = _need_transition(group, request, purpose, now, state.limits, effects)
        elif group.attempts and not all(a.stage == "ambiguous" and _compatible(a, step) for a in group.attempts):
            group = replace(group, status="reconciling")
            effects.append(Observe(group.spec.id))
        elif len(group.attempts) >= 64:
            group = replace(group, status="attempt_limit")
            effects.append(Observe(group.spec.id))
        elif not _admissible(_put(state, group), group, step, now) or (purpose == "optimisation" and not _policy_send_valid(state, group, request, now)):
            group = replace(group, status="physical_scope_blocked")
        else:
            send_by = min(now + state.limits.dispatch_window_ms, request.valid_until_ms,
                          group.observation.valid_until_ms, state.frame.valid_until_ms, group.grant.expires_at_ms)
            if purpose == "optimisation" and _is_battery(state, group):
                send_by = min(send_by, state.conditions.valid_until_ms)
            count = min(group.consecutive_attempts + 1, 32)
            delay = min(state.limits.retry_max_ms, state.limits.retry_base_ms * 2 ** min(count - 1, 20))
            attempt = Attempt(f"{group.spec.id}:{group.next_attempt}", state.revision + 1, group.generation,
                              request.id, request.revision, purpose, group.plan.index, step,
                              group.observation.revision, now, send_by, now + step.confirmation_timeout_ms,
                              send_by + step.latest_effect_delay_ms, "prepared", group.grant)
            group = replace(group, attempts=(*group.attempts, attempt), next_attempt=group.next_attempt + 1,
                            retry_not_before_ms=now + delay, consecutive_attempts=count, status="awaiting_durability")
        state = _put(state, group)
    return state


def reduce_home(state: HomeState, event: Event, now_ms: int) -> tuple[HomeState, tuple[Effect, ...]]:
    """Process one validated domain event; performs no I/O and never reads a clock."""
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("now_ms must be an absolute nonnegative integer")
    if not isinstance(event, (AuthorityInstalled, ConditionsObserved, GrantConfirmed, GrantRevoked, ReleaseApproved, PolicyOffered, MeterObserved, LedgerPruned, Observed, FrameObserved, AuthorityChanged, Requested, Proposed, TransitionFailed, JournalDurable, JournalFailed, TransportResult, Tick)):
        raise ValueError("unsupported runtime event")
    previous = state
    rollback = now_ms < state.last_time_ms
    effects = []
    if rollback:
        # Clock changes cannot erase authority withdrawal or transport evidence.
        # Discard freshness and unsent plans; resume only from new observations.
        state = replace(state, frame=None, conditions=None, resume_after_ms=state.last_time_ms, groups=tuple(
            replace(_supersede(group), observation=None) for group in state.groups))
        effects.extend((Report("home", "clock_rollback"), WakeAt(state.last_time_ms)))
    if isinstance(event, AuthorityInstalled):
        _validate_authority(state, event.authority)
        _integer(event.revision)
        if event.revision == state.authority_revision and event.authority != state.authority:
            raise ValueError("conflicting authority installation revision")
        if event.revision > state.authority_revision:
            if (state.authority and event.authority.scope != state.authority.scope
                    and event.authority.scope.revision == state.authority.scope.revision):
                raise ValueError("changed execution scope needs a new identity")
            state = replace(state, authority=event.authority, authority_revision=event.revision, conditions=None,
                            groups=tuple(replace(_supersede(g), grant_confirmed=False) for g in state.groups))
    elif isinstance(event, ConditionsObserved):
        conditions = event.conditions
        if conditions.at_ms > now_ms or conditions.at_ms < state.resume_after_ms:
            raise ValueError("conditions outside current clock epoch")
        if conditions.revision == state.conditions_revision and state.conditions is not None and conditions != state.conditions:
            raise ValueError("conflicting conditions revision")
        if conditions.revision > state.conditions_revision:
            if state.conditions and conditions.at_ms < state.conditions.at_ms:
                raise ValueError("conditions time regressed")
            state = replace(state, conditions=None if rollback else conditions, conditions_revision=conditions.revision)
    elif isinstance(event, PolicyOffered):
        try:
            state = _accept_policy(state, event, now_ms)
        except ValueError as error:
            effects.append(Report("home", "policy_rejected: " + str(error)))
    elif isinstance(event, (MeterObserved, LedgerPruned)):
        if state.ledger is None:
            raise ValueError("energy ledger is not configured")
        if isinstance(event, MeterObserved):
            policy = state.policy
            ledger, settled, outcome = record_actuals(state.ledger, event.sample, now_ms,
                origin=policy.watermark if policy else None, settled=policy.settled_actuals if policy else ())
            state = replace(state, ledger=ledger,
                            policy=replace(policy, settled_actuals=settled) if policy else None)
            effects.append(Report(event.sample.stream_id, outcome))
        else:
            _integer(event.before_ms)
            if event.before_ms > now_ms:
                raise ValueError("future ledger retention cut")
            if state.policy:
                ledger, settled = settle_and_prune(state.ledger, state.policy.watermark,
                                                   state.policy.settled_actuals, event.before_ms)
                state = replace(state, ledger=ledger, policy=replace(state.policy, settled_actuals=settled))
            else:
                state = replace(state, ledger=prune_ledger(state.ledger, event.before_ms))
    elif isinstance(event, FrameObserved):
        if event.frame.at_ms > now_ms or (not rollback and event.frame.at_ms < state.resume_after_ms):
            raise ValueError("future physical frame")
        if state.frame is None or event.frame.revision > state.frame.revision:
            state = replace(state, frame=None if rollback else event.frame)
    elif isinstance(event, (JournalDurable, JournalFailed)):
        if type(event.revision) is not int or not 0 <= event.revision <= state.revision:
            raise ValueError("invalid journal revision")
        if isinstance(event, JournalFailed):
            for group in state.groups:
                if any(a.stage == "prepared" and a.prepared_revision == event.revision for a in group.attempts):
                    state = _put(state, replace(group, journal_fault=True))
    elif not isinstance(event, Tick):
        group = next((g for g in state.groups if g.spec.id == event.group_id), None)
        if group is None:
            raise ValueError("unknown actuator group")
        if isinstance(event, Observed):
            observation = event.observation
            if observation.at_ms > now_ms or (not rollback and observation.at_ms < state.resume_after_ms) or set(dict(observation.controls)) != set(group.spec.control_keys) or not _within(observation.envelope, group.spec.maximum):
                raise ValueError("observation exceeds its declared group scope")
            if observation.revision > group.observation_revision:
                if group.observation and observation.at_ms < group.observation.at_ms:
                    raise ValueError("observation time regressed")
                group = replace(group, observation=None if rollback else observation, observation_revision=observation.revision)
        elif isinstance(event, GrantConfirmed):
            grant = event.grant
            if grant.epoch < group.grant_epoch or (grant.epoch == group.grant_epoch and group.grant is None):
                effects.append(Report(group.spec.id, "stale_writer_grant"))
            elif grant.epoch == group.grant_epoch and grant != group.grant:
                raise ValueError("conflicting writer grant epoch")
            else:
                if grant != group.grant:
                    group = _supersede(group)
                group = replace(group, grant=grant, grant_epoch=grant.epoch, grant_confirmed=True)
        elif isinstance(event, GrantRevoked):
            _integer(event.epoch, 1)
            if event.epoch >= group.grant_epoch:
                group = replace(_supersede(group), grant=None, grant_epoch=event.epoch, grant_confirmed=False)
        elif isinstance(event, ReleaseApproved):
            _validate_target(group, event.request)
            if group.release and event.request.revision == group.release.revision and event.request != group.release:
                raise ValueError("conflicting release contract revision")
            if group.release is None or event.request.revision > group.release.revision:
                group = _supersede(group) if group.release_pending else group
                group = replace(group, release=event.request)
        elif isinstance(event, AuthorityChanged):
            if event.mode not in ("monitoring", "planning", "control_verification", "controlling") or type(event.revision) is not int or event.revision < 0:
                raise ValueError("invalid requested mode")
            if event.approved_release:
                _validate_target(group, event.approved_release)
                if group.release and event.approved_release.revision == group.release.revision and event.approved_release != group.release:
                    raise ValueError("conflicting release contract revision")
            if event.revision == group.mode_revision and event.mode != group.mode:
                raise ValueError("conflicting requested mode revision")
            if event.revision >= group.mode_revision:
                changed = event.mode != group.mode or event.revision != group.mode_revision
                pending = group.release_pending or (event.mode != "controlling" and (group.owned or any(a.stage != "prepared" for a in group.attempts)))
                group = _supersede(group) if changed else group
                release = event.approved_release if event.approved_release and (group.release is None or event.approved_release.revision > group.release.revision) else group.release
                group = replace(group, mode=event.mode, mode_revision=event.revision, authority_confirmed=True,
                                release=release, release_pending=pending,
                                desired=None if changed and not _is_battery(state, group) else group.desired)
        elif isinstance(event, Requested):
            if _is_battery(state, group):
                raise ValueError("direct request cannot overwrite the scoped battery policy owner")
            if event.mode_revision != group.mode_revision or not group.authority_confirmed:
                effects.append(Report(group.spec.id, "stale_request_authority"))
            else:
                _validate_target(group, event.request)
                if group.desired and event.request.revision == group.desired.revision and event.request != group.desired:
                    raise ValueError("conflicting request at the same revision")
                if group.desired is None or event.request.revision > group.desired.revision:
                    group = replace(_supersede(group), desired=event.request)
        elif isinstance(event, Proposed):
            request, purpose = _request(group, now_ms)
            job = group.transition_work
            valid = (isinstance(job, TransitionJob) and type(event.token) is int and event.token == job.token
                     and now_ms < job.deadline_ms and request is not None
                     and job.key == _transition_key(group, request, purpose)
                     and event.generation == group.generation and event.request_id == request.id
                     and event.request_revision == request.revision and _fresh(group.observation, now_ms)
                     and event.observed_revision == group.observation.revision and event.adapter_revision == group.spec.adapter_revision)
            if valid and group.plan is None:
                group = replace(group, plan=_validate_proposal(group, event, request, purpose), transition_work=None)
        elif isinstance(event, TransitionFailed):
            _integer(event.token, 1)
            _identity(event.reason)
            if event.outcome not in ("retryable", "unsupported"):
                raise ValueError("invalid transition failure outcome")
            job = group.transition_work
            if isinstance(job, TransitionJob) and job.token == event.token and now_ms < job.deadline_ms:
                work = _transition_failure(job, now_ms, state.limits, event.reason,
                                           unsupported=event.outcome == "unsupported")
                group = replace(group, transition_work=work)
                effects.append(Report(group.spec.id, event.reason))
        elif isinstance(event, TransportResult):
            _identity(event.evidence)
            if event.outcome not in ("not_sent", "accepted", "ambiguous"):
                raise ValueError("unsupported transport evidence")
            attempt = next((a for a in group.attempts if a.id == event.attempt_id), None)
            if attempt is not None and attempt.stage != "prepared":
                if event.outcome == "not_sent":
                    group = replace(group, attempts=tuple(a for a in group.attempts if a.id != attempt.id))
                elif not (attempt.stage == "ambiguous" and event.outcome == "accepted"):
                    group = replace(group, attempts=tuple(replace(a, stage=event.outcome) if a.id == attempt.id else a for a in group.attempts))
        state = _put(state, group)
    state = _refresh_policy_decision(state, now_ms, effects)
    if not rollback:
        state = _drive(state, now_ms, event.revision if isinstance(event, JournalDurable) else None, effects)
    changed = _durable_view(state) != _durable_view(previous)
    state = replace(state, revision=previous.revision + int(changed), last_time_ms=max(previous.last_time_ms, now_ms))
    if changed:
        effects.insert(0, Persist(state))
    deadlines = []
    for group in state.groups:
        request, _ = _request(group, now_ms)
        if request:
            deadlines.extend([request.valid_until_ms, group.retry_not_before_ms])
        if request and isinstance(group.transition_work, TransitionJob):
            deadlines.append(group.transition_work.deadline_ms)
        if request and isinstance(group.transition_work, TransitionFailure) and group.transition_work.retry_at_ms is not None:
            deadlines.append(group.transition_work.retry_at_ms)
        if group.observation:
            deadlines.append(group.observation.valid_until_ms)
        for attempt in group.attempts:
            deadlines.extend([attempt.send_by_ms, attempt.confirmation_deadline_ms, attempt.latest_effect_ms])
    if state.policy:
        summary = state.policy.compiled.summary
        deadlines.extend((summary.until_ms, summary.refresh_after_ms))
        if state.policy.candidate_id:
            deadlines.append(state.policy.candidate_since_ms + 5000)
    if state.conditions:
        deadlines.append(state.conditions.valid_until_ms)
    for group in state.groups:
        if group.grant:
            deadlines.append(group.grant.expires_at_ms)
    if state.frame:
        deadlines.append(state.frame.valid_until_ms)
    future = [deadline for deadline in deadlines if deadline > now_ms]
    if future:
        effects.append(WakeAt(min(future)))
    # Coalesce identical requests within this decision; the host also coalesces work.
    return state, tuple(dict.fromkeys(effects))
