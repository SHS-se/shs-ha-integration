"""Pure, offline command reconciliation protocol; not connected to live controls."""
from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite
from typing import Literal, Optional, Union

if __package__:
    from .battery_policy import BatteryPolicy, canonical_json, parse_problem, rank_policy
    from .runtime_json import read_runtime_json
    from .energy_ledger import (
        EnergyLedger, CounterSample, ActualsWatermark, StreamActuals, SettledActuals,
        actuals_since, record_actuals, prune_ledger, start_settlement,
        settle_and_prune, reconciled_actuals, validate_settlement,
    )
else:
    from battery_policy import BatteryPolicy, canonical_json, parse_problem, rank_policy
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

    def __post_init__(self):
        for value in (self.revision, self.at_ms, self.valid_until_ms):
            _integer(value)
        if self.revision < 0 or not 0 <= self.at_ms < self.valid_until_ms:
            raise ValueError("invalid physical frame")


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
        for value in (self.mode_revision, self.generation, self.retry_not_before_ms, self.consecutive_attempts):
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
class PolicyContext:
    revision: int
    problem_json: str
    mode_revision: int
    observation_revision: int
    frame_revision: int
    watermark: ActualsWatermark

    def __post_init__(self):
        for value in (self.revision, self.mode_revision, self.observation_revision, self.frame_revision):
            _integer(value)
        if self.problem_json != canonical_json(parse_problem(read_runtime_json(self.problem_json))):
            raise ValueError("policy context must contain canonical supported problem data")


@dataclass(frozen=True)
class PolicyBinding:
    alternative_id: str
    adapter_revision: str
    response_evidence: str
    current_path_json: str
    target: Controls

    def __post_init__(self):
        for value in (self.alternative_id, self.adapter_revision, self.response_evidence):
            _identity(value)
        _pairs(self.target)


@dataclass(frozen=True)
class PolicySession:
    revision: int
    compiled: BatteryPolicy
    watermark: ActualsWatermark
    bindings: tuple[PolicyBinding, ...]
    context_revision: int
    selected_id: str
    request_id: str
    reconciled_from: Optional[ActualsWatermark]
    reconciled_actuals: tuple[StreamActuals, ...]
    status: Literal["active", "diagnostic_only", "outside_coverage", "awaiting_context"]
    settled_actuals: tuple[SettledActuals, ...]

    def __post_init__(self):
        _integer(self.revision)
        _integer(self.context_revision)
        _identity(self.selected_id)
        _identity(self.request_id)
        if self.selected_id not in {a.id for a in self.compiled.summary.alternatives}:
            raise ValueError("selection is not in the accepted policy")


@dataclass(frozen=True)
class HomeState:
    revision: int
    last_time_ms: int
    limits: Limits
    groups: tuple[Group, ...]
    frame: Optional[Frame] = None
    resume_after_ms: int = 0
    ledger: Optional[EnergyLedger] = None
    policy_context: Optional[PolicyContext] = None
    policy: Optional[PolicySession] = None

    def __post_init__(self):
        for value in (self.revision, self.last_time_ms, self.resume_after_ms):
            _integer(value)
        if not 0 < len(self.groups) <= 32 or len({g.spec.id for g in self.groups}) != len(self.groups):
            raise ValueError("home needs 1..32 unique groups")
        if self.revision < 0 or self.last_time_ms < 0:
            raise ValueError("invalid home revision/time")


# Events are immutable evidence, not executable callbacks.
@dataclass(frozen=True)
class PolicyContextChanged:
    context: PolicyContext


@dataclass(frozen=True)
class PolicyOffered:
    revision: int
    compiled: BatteryPolicy
    watermark: ActualsWatermark
    bindings: tuple[PolicyBinding, ...]
    deadband_sek: float = 0.0


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


Event = Union[PolicyContextChanged, PolicyOffered, MeterObserved, LedgerPruned, Observed, FrameObserved, AuthorityChanged, Requested, Proposed, TransitionFailed, JournalDurable, JournalFailed, TransportResult, Tick]


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


Effect = Union[Persist, Send, Observe, ConfirmAuthority, NeedTransition, WakeAt, Report]


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
    if group.mode == "controlling" and group.desired is not None and now < group.desired.valid_until_ms:
        return group.desired, "optimisation"
    if group.owned or group.attempts:
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
    if not _fresh(state.frame, now):
        return False
    imported, exported = state.frame.external.import_w, state.frame.external.export_w
    for item in state.groups:
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
    imported = state.frame.external.import_w + sum(reservation(g, now).import_w for g in state.groups)
    exported = state.frame.external.export_w + sum(reservation(g, now).export_w for g in state.groups)
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


def _policy_context_matches(state, compiled, now):
    summary, context = compiled.summary, state.policy_context
    if (context is None or now != summary.anchor_ms or state.last_time_ms > now
            or context.problem_json != summary.problem_json or len(state.groups) != 1):
        return False
    group = state.groups[0]
    observation, frame = group.observation, state.frame
    return (group.spec.id == summary.group_id and group.authority_confirmed
            and group.mode_revision == context.mode_revision
            and _fresh(observation, now) and observation.revision == context.observation_revision
            and _fresh(frame, now) and frame.revision == context.frame_revision
            and frame.external == Envelope(summary.external_import_w, summary.external_export_w)
            and frame.limits == Envelope(summary.grid_import_limit_w, summary.grid_export_limit_w)
            and all(dict(observation.measurements).get(key) == value for key, value in summary.expected_measurements))


def _validate_policy_bindings(group, compiled, bindings):
    summary = compiled.summary
    if group.spec.adapter_revision != "synthetic-imposed-power-v1":
        raise ValueError("native response has not been commissioned for this policy version")
    if len(bindings) != len(summary.alternatives) or len({b.alternative_id for b in bindings}) != len(bindings):
        raise ValueError("every alternative needs exactly one local response binding")
    for binding in bindings:
        alternative = next((a for a in summary.alternatives if a.id == binding.alternative_id), None)
        if (alternative is None or binding.adapter_revision != group.spec.adapter_revision
                or binding.current_path_json != alternative.current_path_json):
            raise ValueError("response binding does not match the compiled alternative")
        path = read_runtime_json(alternative.current_path_json)
        target = {key: value for key, value in path["actions"][0].items() if key != "kind"}
        target["pv_curtail_w"] = path["pv_curtail_w"][0]
        # Only this explicit synthetic imposed-flow surface is supported. This
        # does not translate physical watts to native mode/ceiling commands.
        if dict(binding.target) != target or set(target) != set(group.spec.control_keys) or target["pv_curtail_w"] != 0:
            raise ValueError("unsupported synthetic response target")
        possible = Envelope(target["charge_w"], target["discharge_w"])
        if not _within(possible, group.spec.maximum):
            raise ValueError("bound target exceeds declared group capability")


def _accept_policy(state, event, now):
    _integer(event.revision)
    if state.policy and event.revision <= state.policy.revision:
        raise ValueError("stale policy revision")
    if not _policy_context_matches(state, event.compiled, now):
        raise ValueError("outside exact policy coverage")
    context, summary, group = state.policy_context, event.compiled.summary, state.groups[0]
    if state.ledger is None or event.watermark != context.watermark or event.watermark.at_ms != summary.anchor_ms:
        raise ValueError("policy needs the matching real actuals watermark")
    if event.watermark.ledger_revision != state.ledger.revision:
        raise ValueError("meter evidence changed since the exact compilation anchor")
    actuals_since(state.ledger, event.watermark, now)
    reconciliation = reconciled_actuals(state.ledger, state.policy.watermark, state.policy.settled_actuals, now) if state.policy else ()
    if group.attempts:
        raise ValueError("pending physical effects are outside this compiler's coverage")
    _validate_policy_bindings(group, event.compiled, event.bindings)
    for binding in event.bindings:
        target = dict(binding.target)
        if not _room(state, group, Envelope(target["charge_w"], target["discharge_w"]), now):
            raise ValueError("an alternative lacks current physical headroom")
    current_target = group.desired.target if group.desired and now < group.desired.valid_until_ms else group.observation.controls
    current_ids = sorted(b.alternative_id for b in event.bindings if _same(b.target, current_target))
    selected, _ = rank_policy(event.compiled, current_ids[0] if current_ids else None, event.deadband_sek)
    binding = next(b for b in event.bindings if b.alternative_id == selected)
    request_id = f"policy:{event.revision}"
    session = PolicySession(event.revision, event.compiled, event.watermark, event.bindings,
                            context.revision, selected, request_id, state.policy.watermark if state.policy else None, reconciliation,
                            "active" if group.mode == "controlling" else "diagnostic_only", start_settlement(state.ledger, event.watermark))
    if group.mode == "controlling":
        guards = tuple(Guard(key, value, value) for key, value in summary.expected_measurements)
        request = Request(request_id, event.revision, summary.anchor_ms + 1, binding.target, guards)
        group = replace(_supersede(group), desired=request)
        state = _put(state, group)
    return replace(state, policy=session)


def validate_policy_settlement(state):
    validate_settlement(state.ledger, state.policy.watermark, state.policy.settled_actuals)
    if any(prefix.through_ms > state.last_time_ms for prefix in state.policy.settled_actuals):
        raise ValueError("settlement exceeds checkpoint time")


def _fence_policy(state, now, effects):
    session = state.policy
    if session is None or session.status != "active":
        return state
    context = state.policy_context
    valid = (context is not None and context.revision == session.context_revision
             and _policy_context_matches(state, session.compiled, now)
             and state.ledger is not None and state.ledger.revision == session.watermark.ledger_revision)
    if valid:
        return state
    for group in state.groups:
        if group.desired and group.desired.id == session.request_id:
            state = _put(state, replace(_supersede(group), desired=None))
    effects.append(Report("home", "policy_needs_recompile"))
    return replace(state, policy=replace(session, status="outside_coverage"))


def _drive(state, now, durable_revision, effects):
    for original in state.groups:
        group = _settle(original, now)
        request, purpose = _request(group, now)
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
        if not _fresh(group.observation, now) or not _fresh(state.frame, now):
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
                                owned=purpose == "optimisation", consecutive_attempts=0)
            state = _put(state, group)
            continue
        if group.plan and group.plan.index == len(group.plan.steps):
            group = replace(group, plan=None, transition_work=None)
        prepared = next((a for a in group.attempts if a.stage == "prepared"), None)
        if prepared is not None:
            step = prepared.step
            valid = (prepared.generation == group.generation and now < prepared.send_by_ms
                     and _same(group.observation.controls, step.before) and _guards(step.native_guards, group.observation)
                     and _admissible(_put(state, group), group, step, now))
            if not valid:
                group = replace(group, attempts=tuple(a for a in group.attempts if a.id != prepared.id), plan=None, transition_work=None, status="reconciling")
                group = _need_transition(group, request, purpose, now, state.limits, effects)
            elif durable_revision == prepared.prepared_revision:
                attempt = replace(prepared, stage="sent", confirmation_deadline_ms=now + step.confirmation_timeout_ms)
                group = replace(group, attempts=tuple(attempt if a.id == prepared.id else a for a in group.attempts), owned=True, status="executing", journal_fault=False)
                effects.append(Send(group.spec.id, attempt.id, step.key, step.value, attempt.send_by_ms))
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
        elif not _admissible(_put(state, group), group, step, now):
            group = replace(group, status="physical_scope_blocked")
        else:
            send_by = min(now + state.limits.dispatch_window_ms, request.valid_until_ms,
                          group.observation.valid_until_ms, state.frame.valid_until_ms)
            count = min(group.consecutive_attempts + 1, 32)
            delay = min(state.limits.retry_max_ms, state.limits.retry_base_ms * 2 ** min(count - 1, 20))
            attempt = Attempt(f"{group.spec.id}:{group.next_attempt}", state.revision + 1, group.generation,
                              request.id, request.revision, purpose, group.plan.index, step,
                              group.observation.revision, now, send_by, now + step.confirmation_timeout_ms,
                              send_by + step.latest_effect_delay_ms, "prepared")
            group = replace(group, attempts=(*group.attempts, attempt), next_attempt=group.next_attempt + 1,
                            retry_not_before_ms=now + delay, consecutive_attempts=count, status="awaiting_durability")
        state = _put(state, group)
    return state


def reduce_home(state: HomeState, event: Event, now_ms: int) -> tuple[HomeState, tuple[Effect, ...]]:
    """Process one validated domain event; performs no I/O and never reads a clock."""
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("now_ms must be an absolute nonnegative integer")
    if not isinstance(event, (PolicyContextChanged, PolicyOffered, MeterObserved, LedgerPruned, Observed, FrameObserved, AuthorityChanged, Requested, Proposed, TransitionFailed, JournalDurable, JournalFailed, TransportResult, Tick)):
        raise ValueError("unsupported runtime event")
    previous = state
    rollback = now_ms < state.last_time_ms
    effects = []
    if rollback:
        # Clock changes cannot erase authority withdrawal or transport evidence.
        # Discard freshness and unsent plans; resume only from new observations.
        state = replace(state, frame=None, resume_after_ms=state.last_time_ms, groups=tuple(
            replace(_supersede(group), observation=None) for group in state.groups))
        effects.extend((Report("home", "clock_rollback"), WakeAt(state.last_time_ms)))
    if isinstance(event, PolicyContextChanged):
        context = event.context
        if state.policy_context and context.revision == state.policy_context.revision and context != state.policy_context:
            raise ValueError("conflicting policy context revision")
        if state.policy_context is None or context.revision > state.policy_context.revision:
            state = replace(state, policy_context=context)
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
        elif isinstance(event, AuthorityChanged):
            if event.mode not in ("monitoring", "planning", "control_verification", "controlling") or type(event.revision) is not int or event.revision < 0:
                raise ValueError("invalid operating authority")
            if event.approved_release:
                _validate_target(group, event.approved_release)
            if event.revision == group.mode_revision and group.authority_confirmed and (event.mode != group.mode or event.approved_release != group.release):
                raise ValueError("conflicting authority at the same revision")
            if event.revision >= group.mode_revision:
                changed = event.mode != group.mode or event.revision != group.mode_revision
                group = _supersede(group) if changed else group
                group = replace(group, mode=event.mode, mode_revision=event.revision, authority_confirmed=True, release=event.approved_release,
                                desired=None if changed else group.desired)
        elif isinstance(event, Requested):
            if state.policy and state.policy.status == "active":
                raise ValueError("direct request cannot overwrite an active policy decision")
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
    state = _fence_policy(state, now_ms, effects)
    if not rollback:
        state = _drive(state, now_ms, event.revision if isinstance(event, JournalDurable) else None, effects)
    changed = state != previous
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
    if state.frame:
        deadlines.append(state.frame.valid_until_ms)
    future = [deadline for deadline in deadlines if deadline > now_ms]
    if future:
        effects.append(WakeAt(min(future)))
    # Coalesce identical requests within this decision; the host also coalesces work.
    return state, tuple(dict.fromkeys(effects))
