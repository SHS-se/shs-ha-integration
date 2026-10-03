"""Pure, offline command reconciliation protocol; not connected to live controls."""
from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite
import json
from typing import Literal, Optional, Union

from .battery_physical import ExecutionConditions, ContextIdentity, Permissions, BatteryPlant, BatteryOperation, OPERATION_CEILINGS, CHARGE_PERMISSIONS
from . import plan_execution as execution
from .native_records import (
    Controls, Envelope, Guard, Measurements, NativeCatalog, NeedTransition, Observation, OperationBinding, Proposed, Purpose, ReliefRule, Request, Send, Step, Value, WriterGrant, WriterIdentity, _identity, _integer, _number, _pairs, _reason, _within
)
from .battery_supply import SupplyScope
from .runtime_json import read_runtime_json, encode_value
from .energy_ledger import (
    EnergyLedger, CounterSample, ActualsWatermark, StreamActuals, SettledActuals,
    actuals_since, record_actuals, prune_ledger, start_settlement,
    settle_and_prune, reconciled_actuals, validate_settlement,
)

Mode = Literal["monitoring", "planning", "control_verification", "controlling"]


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
        _reason(self.reason)
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
                or self.identity.response_model_revision not in ("pv-first-v1", "pv-first-dc-v2")
                or self.catalog.charge_max_w != self.plant.charge_max_w
                or self.catalog.discharge_max_w != self.plant.discharge_max_w):
            raise ValueError("configured scope/catalog/plant identity mismatch")


# Traces are recent diagnostic evidence; the account keeps the complete
# accounting record. Every battery refresh and command result adds a trace
# (about 5,000 an hour), so only the latest are retained, in memory, on disk
# and in downloads. Trim in batches to avoid repeatedly copying the bounded
# immutable window; the database deletes the same oldest rows in its commit.
MAX_EXECUTION_TRACES = 8192
TRACE_TRIM = 1024
TRACE_RETENTION_MS = 3 * 86400000


def retain_traces(traces):
    """The latest traces; once over the limit, the oldest leave in TRACE_TRIM blocks."""
    if traces:
        cutoff = traces[-1].at_ms - TRACE_RETENTION_MS
        first = next((i for i, row in enumerate(traces) if row.at_ms >= cutoff), len(traces))
        traces = traces[first:]
    excess = len(traces) - MAX_EXECUTION_TRACES
    if excess <= 0:
        return traces
    return traces[-(-excess // TRACE_TRIM) * TRACE_TRIM:]


@dataclass(frozen=True)
class ExecutionTrace:
    at_ms: int
    account_receipt: int
    observation_count: int
    reconciliation_count: int
    reference_id: Optional[str]
    input_json: str
    effects_json: str
    live: Optional[execution.LiveState]
    assessment: Optional[execution.Assessment]
    conversion_json: Optional[str]


@dataclass(frozen=True)
class PlanRejection:
    at_ms: int
    contract_id: Optional[str]
    generation: Optional[int]
    reason: str


@dataclass(frozen=True)
class ExecutionSession:
    account: execution.Account = execution.Account()
    assessment: Optional[execution.Assessment] = None
    status: str = "awaiting_context"
    request_id: Optional[str] = None
    replan_reason: Optional[str] = None
    captured_feedback: Optional[str] = None
    traces: tuple[ExecutionTrace, ...] = ()
    live: Optional[execution.LiveState] = None
    plan_rejection: Optional[PlanRejection] = None


def record_plan_rejection(session: ExecutionSession, rejection: PlanRejection) -> ExecutionSession:
    """Keep current rejection until a newer reference is actually admitted."""
    accepted = session.account.contract
    if accepted and rejection.generation is not None and rejection.generation < accepted.generation:
        return session
    return replace(session, plan_rejection=rejection)


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
    execution: ExecutionSession = ExecutionSession()

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
class ExecutionPlanOffered:
    contract: execution.ExecutionContract


@dataclass(frozen=True)
class ExecutionPlanRejected:
    rejection: PlanRejection


@dataclass(frozen=True)
class ReplanRequested:
    observation: execution.StateObservation
    scope_revision: str


@dataclass(frozen=True)
class CounterReceived:
    sample: execution.MeterReceipt


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
class MeasurementsObserved:
    """One physical capture: never decide using half of a new measurement."""
    observed: Observed
    frame: Frame
    conditions: ExecutionConditions


@dataclass(frozen=True)
class MeasurementsReceived:
    """Atomic physical evidence; economic selection belongs to an explicit decision."""
    observed: Observed
    frame: Frame
    conditions: ExecutionConditions


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
    """Request an economic decision."""


@dataclass(frozen=True)
class ExecutionWake:
    """Progress command deadlines without choosing another economic target."""


Event = Union[AuthorityInstalled, ConditionsObserved, GrantConfirmed, GrantRevoked, ReleaseApproved, ExecutionPlanOffered, ExecutionPlanRejected, ReplanRequested, CounterReceived, MeterObserved, LedgerPruned, Observed, FrameObserved, MeasurementsObserved, MeasurementsReceived, AuthorityChanged, Requested, Proposed, TransitionFailed, JournalDurable, JournalFailed, TransportResult, Tick, ExecutionWake]


@dataclass(frozen=True)
class Persist:
    state: HomeState


@dataclass(frozen=True)
class NeedPlan:
    reason: str


@dataclass(frozen=True)
class Observe:
    group_id: str


@dataclass(frozen=True)
class ConfirmAuthority:
    group_id: str


@dataclass(frozen=True)
class WakeAt:
    at_ms: int


@dataclass(frozen=True)
class Report:
    group_id: str
    reason: str


Effect = Union[NeedPlan, Persist, Send, Observe, ConfirmAuthority, NeedTransition, WakeAt, Report]


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
    # Owning the controls is no reason to change them. Without a current target
    # they keep the last setting; only an explicit release hands them back. Do
    # not restore a fallback to the release target (docs/control-continuity.md).
    if group.release_pending and group.release is not None and now < group.release.valid_until_ms:
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


def command_controls(group):
    """Acknowledged assignments for this sequence, never physical measurements."""
    if group.plan is not None and group.plan.index:
        return group.plan.steps[group.plan.index - 1].after
    accepted = [a for a in group.attempts if a.stage == "accepted"]
    if accepted:
        return accepted[-1].step.after
    return group.observation.controls if group.observation else ()


def _settle(group, now):
    observation = group.observation
    remaining = []
    plan = group.plan
    for attempt in group.attempts:
        # A fresh sample after the adapter's last possible effect settles old effects,
        # whether the setting matches current intent or represents external drift.
        acknowledged = (attempt.stage == "accepted"
                        and _fresh(observation, now) and observation.revision > attempt.observed_revision
                        and _same(observation.controls, command_controls(group)))
        settled = acknowledged or (attempt.stage in ("sent", "ambiguous") and _fresh(observation, now)
                   and observation.at_ms >= attempt.latest_effect_ms
                   and observation.revision > attempt.observed_revision)
        if settled:
            if attempt.stage != "accepted" and preparation_matches(plan, attempt):
                if _same(observation.controls, attempt.step.after) and _guards(attempt.step.native_guards, observation):
                    plan = replace(plan, index=plan.index + 1)
                else:
                    plan = None
        else:
            if attempt.stage == "sent" and now >= attempt.confirmation_deadline_ms:
                attempt = replace(attempt, stage="ambiguous")
            remaining.append(attempt)
    # A retry is unsent software work. Advancing or invalidating its sequence
    # must cancel it in the same state update; issued effects remain independent.
    remaining = tuple(a for a in remaining if a.stage != "prepared" or preparation_matches(plan, a))
    # Keep completed command progress while HA publication lags. Once normal
    # reports show the final settings, observations can detect later changes.
    if (plan is not None and plan.index == len(plan.steps) and not remaining
            and _fresh(observation, now) and _same(observation.controls, plan.steps[-1].after)):
        plan = None
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
    previous = command_controls(group)
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


def _same_transition(left, right):
    # A new power/SOC capture does not replace a request or its adapter job.
    return replace(left, observed_revision=0) == replace(right, observed_revision=0)


def _transition_failure(job, now, limits, reason, *, unsupported=False):
    delay = min(limits.retry_max_ms, limits.retry_base_ms * 2 ** min(job.attempt - 1, 20))
    return TransitionFailure(job.key, job.attempt, None if unsupported else now + delay, reason)


def resumed_transition_work(group, now, limits):
    work = group.transition_work
    if isinstance(work, TransitionJob):
        # The old host and its worker are gone. Pace the retry from recovery,
        # not from the lifetime of the interrupted worker's input evidence.
        return _transition_failure(work, now, limits, "transition_interrupted")
    return work


def _need_transition(group, request, purpose, now, limits, effects):
    key = _transition_key(group, request, purpose)
    work = group.transition_work
    if isinstance(work, TransitionJob) and _same_transition(work.key, key):
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
    # The host bounds adapter execution time. Queueing and journal writes do
    # not consume that budget; a result remains usable only with this exact
    # request and fresh observation (also rechecked when admitting Proposed).
    job = TransitionJob(key, group.next_transition, attempt,
                        min(request.valid_until_ms, group.observation.valid_until_ms))
    effects.append(NeedTransition(group.spec.id, group.generation, purpose, request,
                                 group.observation, group.spec.adapter_revision, job.token, job.deadline_ms, command_controls(group)))
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


def execution_live(state, now):
    c, a = state.conditions, state.authority
    pending = max(0, state.frame.external.import_w - c.residual_load_w) if state.frame else 0
    return execution.LiveState(now, round(c.energy_kwh * 1e6), c.residual_load_w, c.pv_w,
        c.eligible_load_w if c.eligible_load_w is not None else c.residual_load_w,
        int(a.plant.charge_max_w), int(a.plant.discharge_max_w), a.plant.import_limit_w,
        a.plant.export_limit_w, pending, a.permissions.available,
        a.permissions.grid_charge_allowed, a.permissions.battery_export_allowed,
        round(a.permissions.export_reserve_kwh * 1e6), round(a.plant.cutoff_kwh * 1e6))


def execution_binding(state, assessment):
    """Sized requests carry the assessed power; permissions keep their catalog ceiling.

    Under an automatic mode the plant regulates against real surplus and demand,
    so writing an assessed charge ceiling would only re-forbid, one refresh late,
    what the plant is already entitled to absorb.
    """
    catalog = state.authority.catalog
    base = next(b for b in catalog.bindings if b.operation.operation == assessment.operation)
    charge = (base.operation.charge_limit_w if assessment.operation in CHARGE_PERMISSIONS
              else assessment.charge_dc_w)
    target = tuple((key, charge if key == catalog.charge_key else
                    assessment.discharge_dc_w if key == catalog.discharge_key else value)
                   for key, value in base.target)
    return replace(base, target=target)


def _validate_execution(state, contract):
    a = state.authority
    if a is None or contract is None:
        raise ValueError("plan and local battery authority required")
    # Forecast provenance is not current command authority. Local mode, scope,
    # permissions and physical bounds are installed and checked independently.
    if contract.capacity_mwh != round(a.plant.capacity_kwh * 1e6):
        raise ValueError("plan differs from current battery capacity")


def _withdraw_execution(state, reason, effects, *, refresh=True):
    session, group = state.execution, _battery_group(state)
    if group and group.desired is not None:
        # A lost plan, changed authority or stale measurement holds the last setting.
        state = _put(state, replace(_supersede(group), desired=None))
    if refresh and session.replan_reason != reason:
        effects.append(NeedPlan(reason))
    return replace(state, execution=replace(session, status="unavailable", assessment=None,
                   replan_reason=reason if refresh else session.replan_reason))


def _refresh_execution(state, now, effects, *, select=True):
    session, conditions = state.execution, state.conditions
    contract = session.account.contract
    if contract is None:
        return state
    group = _battery_group(state)
    try:
        _validate_execution(state, contract)
    except ValueError:
        return _withdraw_execution(state, "plan_authority_changed", effects)
    if contract.interval(now) is None:
        return _withdraw_execution(state, "plan_expired", effects)
    if conditions is None or not _fresh(conditions, now):
        effects.append(Observe(group.spec.id))
        return _withdraw_execution(state, "waiting_for_measurements", effects, refresh=False)
    if conditions.identity != state.authority.identity or conditions.permissions != state.authority.permissions:
        return _withdraw_execution(state, "measurement_authority_changed", effects)
    if not select:
        return state
    live = execution_live(state, now)
    assessment = execution.assess_execution(session.account, live, state.authority.plant.conversion)
    if assessment.replan_reason and assessment.replan_reason != session.replan_reason:
        effects.append(NeedPlan(assessment.replan_reason))
    session = replace(session, assessment=assessment, live=live,
        replan_reason=assessment.replan_reason or session.replan_reason,
        status="active" if group.mode == "controlling" else "diagnostic_only")
    binding = execution_binding(state, assessment)
    if group.mode == "controlling":
        if group.release_pending:
            group = replace(_supersede(group), release_pending=False)
        if group.desired and _same(group.desired.target, binding.target) and group.desired.native_guards == binding.native_guards:
            request = replace(group.desired, valid_until_ms=assessment.valid_until_ms)
        else:
            group = _supersede(group)
            request = Request(f"battery:{group.generation}", group.generation,
                              assessment.valid_until_ms, binding.target, binding.native_guards)
        group = replace(group, desired=request)
        session = replace(session, request_id=request.id)
    elif group.desired is not None:
        group = replace(_supersede(group), desired=None,
                        release_pending=group.release_pending or group.owned or bool(group.attempts))
    return replace(_put(state, group), execution=session)


def _execution_send_valid(state, group, request, now):
    if not _is_battery(state, group):
        return True
    session = state.execution
    if session.status != "active" or not _fresh(state.conditions, now):
        return False
    try:
        _validate_execution(state, session.account.contract)
        assessment = execution.assess_execution(session.account, execution_live(state, now),
                                                state.authority.plant.conversion, responsibilities=False)
        binding = execution_binding(state, assessment)
    except (ValueError, StopIteration):
        return False
    return (now < assessment.valid_until_ms and _same(request.target, binding.target)
            and request.native_guards == binding.native_guards
            and state.conditions.identity == state.authority.identity
            and state.conditions.permissions == state.authority.permissions)


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
            and (send.key, send.value) == (attempt.step.key, attempt.step.value)
            and _same(command_controls(group), attempt.step.before)
            and (purpose == "release" or not _is_battery(state, group) or state.execution.status == "active"))


def _durable_view(state):
    """Frequent physical evidence and derived diagnostics do not journal by themselves."""
    session = replace(state.execution, assessment=None, live=None, status="awaiting_context")
    return replace(state, last_time_ms=0, frame=None, conditions=None, conditions_revision=-1, execution=session,
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
        if purpose == "optimisation" and _is_battery(state, group) and state.execution.status != "active":
            state = _put(state, replace(group, status="awaiting_context"))
            continue
        if not group.authority_confirmed:
            effects.append(ConfirmAuthority(group.spec.id))
        if request is None:
            group = replace(_supersede(group), status="holding" if group.owned else "inactive") if group.plan else replace(group, status="holding" if group.owned else "inactive")
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
        if _same(command_controls(group), request.target):
            # Prepared work can be cancelled in-process. Issued work cannot be erased
            # just because the desired setting currently happens to be visible.
            group = replace(group, transition_work=None,
                            attempts=tuple(a for a in group.attempts if a.stage != "prepared"))
            if any(a.stage != "accepted" for a in group.attempts):
                group = replace(group, status="reconciling")
                effects.append(Observe(group.spec.id))
            else:
                group = replace(group, status="adopted" if purpose == "optimisation" else "released",
                                owned=purpose == "optimisation", release_pending=False if purpose == "release" else group.release_pending, consecutive_attempts=0)
            state = _put(state, group)
            continue
        if purpose == "optimisation" and not _execution_send_valid(state, group, request, now):
            # Keep issued effects. Cancel unsent work and wait for the decision
            # clock instead of repeatedly proposing the same invalid target.
            state = _put(state, replace(group, plan=None, transition_work=None,
                attempts=tuple(a for a in group.attempts if a.stage != "prepared"),
                status="awaiting_decision"))
            continue
        prepared = next((a for a in group.attempts if a.stage == "prepared"), None)
        if prepared is not None:
            step = prepared.step
            valid = (prepared.generation == group.generation and prepared.grant == group.grant and now < prepared.send_by_ms
                     and _same(command_controls(group), step.before) and _guards(step.native_guards, group.observation)
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
        if not _same(command_controls(group), step.before) or not _guards(step.native_guards, group.observation):
            group = replace(group, plan=None, transition_work=None, status="reconciling")
            group = _need_transition(group, request, purpose, now, state.limits, effects)
        elif any(a.stage != "accepted" and not (a.stage == "ambiguous" and _compatible(a, step)) for a in group.attempts):
            group = replace(group, status="reconciling")
            effects.append(Observe(group.spec.id))
        elif len(group.attempts) >= 64:
            group = replace(group, status="attempt_limit")
            effects.append(Observe(group.spec.id))
        elif not _admissible(_put(state, group), group, step, now):
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


def _observe_measurement(state, event, now_ms, rollback):
    # Local revisions order updates; source timestamps only bound freshness.
    if isinstance(event, ConditionsObserved):
        conditions = event.conditions
        if conditions.at_ms > now_ms or conditions.at_ms < state.resume_after_ms:
            raise ValueError("conditions outside current clock epoch")
        if conditions.revision == state.conditions_revision and state.conditions is not None and conditions != state.conditions:
            raise ValueError("conflicting conditions revision")
        if conditions.revision > state.conditions_revision:
            state = replace(state, conditions=None if rollback else conditions, conditions_revision=conditions.revision)
    elif isinstance(event, FrameObserved):
        if event.frame.at_ms > now_ms or (not rollback and event.frame.at_ms < state.resume_after_ms):
            raise ValueError("future physical frame")
        if state.frame is None or event.frame.revision > state.frame.revision:
            state = replace(state, frame=None if rollback else event.frame)
    elif isinstance(event, Observed):
        group = next((g for g in state.groups if g.spec.id == event.group_id), None)
        if group is None:
            raise ValueError("unknown actuator group")
        before_commands = command_controls(group)
        observation = event.observation
        if observation.at_ms > now_ms or (not rollback and observation.at_ms < state.resume_after_ms) or set(dict(observation.controls)) != set(group.spec.control_keys) or not _within(observation.envelope, group.spec.maximum):
            raise ValueError("observation exceeds its declared group scope")
        if observation.revision > group.observation_revision:
            group = replace(group, observation=None if rollback else observation, observation_revision=observation.revision)
        if isinstance(group.transition_work, TransitionJob) and not _same(before_commands, command_controls(group)):
            # A route built from different native starting controls is obsolete.
            # Ordinary power/SOC captures retain the worker and its token.
            group = replace(group, transition_work=None)

        state = _put(state, group)
    return state


def _receive_measurements(state, event, now_ms, rollback):
    if isinstance(event, (MeasurementsObserved, MeasurementsReceived)):
        if not (event.observed.observation.at_ms == event.frame.at_ms == event.conditions.at_ms
                and event.observed.observation.valid_until_ms == event.frame.valid_until_ms == event.conditions.valid_until_ms):
            raise ValueError("measurement capture timestamps differ")
        events = (event.observed, FrameObserved(event.frame), ConditionsObserved(event.conditions))
    else:
        events = (event,)
    previous = state.conditions_revision
    for measurement in events:
        state = _observe_measurement(state, measurement, now_ms, rollback)
    if isinstance(event, (ConditionsObserved, MeasurementsObserved, MeasurementsReceived)) and state.conditions is not None and not rollback and state.conditions_revision > previous and _fresh(state.conditions, now_ms):
        observation = execution.StateObservation(state.conditions.stored_at_ms if state.conditions.stored_at_ms is not None else state.conditions.at_ms, round(state.conditions.energy_kwh * 1e6), "live_soc")
        state = replace(state, execution=replace(state.execution,
            account=execution.observe_state(state.execution.account, observation)))
    return state


def _receive_counter(state, event):
    sample = event.sample
    account = execution.record_meter(state.execution.account, **{
        key: getattr(sample, key) for key in sample.__dataclass_fields__ if key != "receipt"})
    return replace(state, execution=replace(state.execution, account=account))


def archive_evidence(state: HomeState, event: Event | None, now_ms: int):
    """Accept physical evidence without economics, command driving or traces.

    Real control/guard changes and command settlement progress promptly.
    The host batches ordinary evidence durability behind the native journal's retained prefix.
    A subsequent command transition commits this evidence before dispatch.
    """
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("now_ms must be an absolute nonnegative integer")
    if event is None:
        return state, ()
    if not isinstance(event, (CounterReceived, MeasurementsObserved, MeasurementsReceived)):
        raise ValueError("unsupported archival event")
    if now_ms < state.last_time_ms:
        # Clock rollback has command-state consequences; retain the established
        # reducer's withdrawal and unsent-work fencing for this exceptional event.
        return reduce_home(state, event, now_ms)
    previous = state
    if isinstance(event, CounterReceived):
        state = _receive_counter(state, event)
    else:
        state = _receive_measurements(state, event, now_ms, False)
    if not isinstance(event, CounterReceived):
        checked = _refresh_execution(state, now_ms, [], select=False)
        control_changed = any(
            before.observation is None or after.observation is None
            or before.observation.controls != after.observation.controls
            or (after.desired is not None and
                _guards(after.desired.native_guards, before.observation) !=
                _guards(after.desired.native_guards, after.observation))
            or _settle(after, now_ms) != after
            or (before.observation.valid_until_ms > previous.last_time_ms) != _fresh(after.observation, now_ms)
            for before, after in zip(previous.groups, state.groups))
        if checked != state or control_changed:
            # Reprocess from the original state so accepted evidence is inserted
            # exactly once, and commit its source prefix before any command.
            return reduce_home(previous, event, now_ms)
    changed = state.execution.account is not previous.execution.account
    return replace(state, revision=previous.revision + int(changed),
                   last_time_ms=max(previous.last_time_ms, now_ms)), ()


def decision_requested(state: HomeState, event: Event) -> bool:
    """Economic inputs, separate from evidence and physical protocol progress."""
    if isinstance(event, AuthorityInstalled):
        return event.revision > state.authority_revision
    if isinstance(event, AuthorityChanged):
        group = next((g for g in state.groups if g.spec.id == event.group_id), None)
        return group is not None and (event.mode, event.revision) != (group.mode, group.mode_revision)
    return isinstance(event, (Tick, ConditionsObserved, FrameObserved, Observed,
                              MeasurementsObserved, ExecutionPlanOffered, ReplanRequested))


def reduce_home(state: HomeState, event: Event, now_ms: int) -> tuple[HomeState, tuple[Effect, ...]]:
    """Process one validated domain event; performs no I/O and never reads a clock."""
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("now_ms must be an absolute nonnegative integer")
    if not isinstance(event, (AuthorityInstalled, ConditionsObserved, GrantConfirmed, GrantRevoked, ReleaseApproved, ExecutionPlanOffered, ExecutionPlanRejected, ReplanRequested, CounterReceived, MeterObserved, LedgerPruned, Observed, FrameObserved, MeasurementsObserved, MeasurementsReceived, AuthorityChanged, Requested, Proposed, TransitionFailed, JournalDurable, JournalFailed, TransportResult, Tick, ExecutionWake)):
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
    elif isinstance(event, (Observed, FrameObserved, ConditionsObserved, MeasurementsObserved, MeasurementsReceived)):
        state = _receive_measurements(state, event, now_ms, rollback)
    elif isinstance(event, ExecutionPlanOffered):
        try:
            _validate_execution(state, event.contract)
            if state.conditions is None or not _fresh(state.conditions, now_ms):
                raise ValueError("fresh state required for plan admission")
            account = execution.admit_plan(state.execution.account, event.contract, now_ms,
                execution.StateObservation(now_ms, round(state.conditions.energy_kwh * 1e6), "live_soc_at_acceptance", False))
            changed = account.contract != state.execution.account.contract
            state = replace(state, execution=replace(state.execution, account=account, replan_reason=None,
                plan_rejection=None if changed else state.execution.plan_rejection))
        except ValueError as error:
            state = replace(state, execution=record_plan_rejection(state.execution,
                PlanRejection(now_ms, event.contract.id, event.contract.generation, str(error))))
            effects.append(Report("home", "plan_rejected: " + str(error)))
            effects.append(NeedPlan("plan_handover_rejected"))
    elif isinstance(event, ExecutionPlanRejected):
        state = replace(state, execution=record_plan_rejection(state.execution, event.rejection))
        effects.append(Report("home", "plan_rejected: " + event.rejection.reason))
    elif isinstance(event, ReplanRequested):
        account = execution.observe_state(state.execution.account, event.observation)
        account, captured = execution.capture_replan(account, now_ms)
        captured.update(scope_revision=event.scope_revision, reason=state.execution.replan_reason)
        from dataclasses import asdict
        captured["pending_effects"] = [asdict(a) for g in state.groups for a in g.attempts]
        state = replace(state, execution=replace(state.execution, account=account,
            captured_feedback=json.dumps(captured, sort_keys=True, allow_nan=False)))
    elif isinstance(event, CounterReceived):
        state = _receive_counter(state, event)
    elif isinstance(event, (MeterObserved, LedgerPruned)):
        if state.ledger is None:
            raise ValueError("energy ledger is not configured")
        if isinstance(event, MeterObserved):
            ledger, _, outcome = record_actuals(state.ledger, event.sample, now_ms)
            state = replace(state, ledger=ledger)
            effects.append(Report(event.sample.stream_id, outcome))
        else:
            state = replace(state, ledger=prune_ledger(state.ledger, event.before_ms))
    elif isinstance(event, (JournalDurable, JournalFailed)):
        if type(event.revision) is not int or not 0 <= event.revision <= state.revision:
            raise ValueError("invalid journal revision")
        if isinstance(event, JournalFailed):
            for group in state.groups:
                if any(a.stage == "prepared" and a.prepared_revision == event.revision for a in group.attempts):
                    state = _put(state, replace(group, journal_fault=True))
    elif not isinstance(event, (Tick, ExecutionWake)):
        group = next((g for g in state.groups if g.spec.id == event.group_id), None)
        if group is None:
            raise ValueError("unknown actuator group")
        if isinstance(event, GrantConfirmed):
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
                # Leaving Controlling, for Verification too, is the release; returning
                # to it cancels one in progress. See docs/control-continuity.md.
                pending = event.mode != "controlling" and (group.release_pending or group.owned or any(a.stage != "prepared" for a in group.attempts))
                group = _supersede(group) if changed else group
                release = event.approved_release if event.approved_release and (group.release is None or event.approved_release.revision > group.release.revision) else group.release
                group = replace(group, mode=event.mode, mode_revision=event.revision, authority_confirmed=True,
                                release=release, release_pending=pending,
                                desired=None if changed and not _is_battery(state, group) else group.desired)
        elif isinstance(event, Requested):
            if _is_battery(state, group):
                raise ValueError("direct request cannot overwrite the scoped battery execution owner")
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
                     and _same_transition(job.key, _transition_key(group, request, purpose))
                     and event.generation == group.generation and event.request_id == request.id
                     and event.request_revision == request.revision and _fresh(group.observation, now_ms)
                     and event.observed_revision == job.key.observed_revision and event.adapter_revision == group.spec.adapter_revision
                     and (not event.steps or _same(event.steps[0].before, command_controls(group))))
            if valid and group.plan is None:
                if len(event.relief_rules)>8 or any(rule.id not in {step.relief_id for step in event.steps} for rule in event.relief_rules):
                    raise ValueError("adapter relief must belong to this bounded proposal")
                retained={a.step.relief_id for a in group.attempts if a.step.relief_id}
                rules=tuple(r for r in group.spec.relief_rules if r.id in retained) if event.relief_rules else group.spec.relief_rules
                rules=tuple({r.id:r for r in (*rules,*event.relief_rules)}.values())
                group=replace(group,spec=replace(group.spec,relief_rules=rules))
                group = replace(group, plan=_validate_proposal(group, event, request, purpose), transition_work=None)
        elif isinstance(event, TransitionFailed):
            _integer(event.token, 1)
            _reason(event.reason)
            if event.outcome not in ("retryable", "unsupported"):
                raise ValueError("invalid transition failure outcome")
            job = group.transition_work
            if isinstance(job, TransitionJob) and job.token == event.token and now_ms < job.deadline_ms:
                work = _transition_failure(job, now_ms, state.limits, event.reason,
                                           unsupported=event.outcome == "unsupported")
                group = replace(group, transition_work=work)
                effects.append(Report(group.spec.id, event.reason))
        elif isinstance(event, TransportResult):
            before_commands = command_controls(group)
            _identity(event.evidence)
            if event.outcome not in ("not_sent", "accepted", "ambiguous"):
                raise ValueError("unsupported transport evidence")
            attempt = next((a for a in group.attempts if a.id == event.attempt_id), None)
            if attempt is not None and attempt.stage != "prepared":
                if event.outcome == "not_sent" and attempt.stage != "accepted":
                    group = replace(group, attempts=tuple(a for a in group.attempts if a.id != attempt.id))
                else:
                    # One service result owns command progression. Queue delay,
                    # duplicate results and superseded requests cannot turn a
                    # completed HA call into an invalid confirmation event.
                    # A route can be proposed again within one request, when the
                    # new setting is reported before its call returns. Only the
                    # step this attempt was prepared for may be advanced; a result
                    # of the replaced route would otherwise skip a different step.
                    plan = group.plan
                    if (event.outcome == "accepted" and attempt.stage != "accepted"
                            and preparation_matches(plan, attempt)):
                        plan = replace(plan, index=plan.index + 1)
                    outcome = "accepted" if attempt.stage == "accepted" else event.outcome
                    group = replace(group, plan=plan,
                        attempts=tuple(replace(a, stage=outcome) if a.id == attempt.id else a for a in group.attempts),
                        retry_not_before_ms=now_ms if event.outcome == "accepted" else group.retry_not_before_ms)
            if (isinstance(group.transition_work, TransitionJob)
                    and not _same(before_commands, command_controls(group))):
                # An older call can finish while a new request's adapter is
                # preparing. Its proposal must use the newly acknowledged start.
                group = replace(group, transition_work=None)
        state = _put(state, group)
    state = _refresh_execution(state, now_ms, effects, select=decision_requested(previous, event))
    if not rollback:
        state = _drive(state, now_ms, event.revision if isinstance(event, JournalDurable) else None, effects)
    if state.authority and (not isinstance(event, JournalDurable) or any(isinstance(e, Send) for e in effects)):
        # Archive the evidence for real evaluations and outgoing/returned native
        # effects. Durability acknowledgements alone never create a journal loop.
        if state != previous or isinstance(event, (Tick, ExecutionPlanOffered)):
            session=state.execution
            contract=session.account.contract
            trace=ExecutionTrace(now_ms,session.account.receipt,len(session.account.observations),
                len(session.account.reconciliations),contract.id if contract else None,
                json.dumps(encode_value(event),sort_keys=True,allow_nan=False),
                json.dumps([encode_value(e) for e in effects if not isinstance(e,Persist)],sort_keys=True,allow_nan=False),
                session.live,session.assessment,
                json.dumps(state.authority.plant.conversion.wire()) if state.authority.plant.conversion else None)
            state=replace(state,execution=replace(session,traces=retain_traces((*session.traces,trace))))
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
    if state.execution.account.contract:
        contract = state.execution.account.contract
        deadlines.extend([contract.valid_until_ms, *(r.end_ms for r in contract.intervals),
                          *(o.deadline_ms for o in contract.objectives)])
        if state.execution.assessment:
            deadlines.append(state.execution.assessment.valid_until_ms)
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
    # Effects are a bounded batch. Their immutable accounting views may be
    # backed by storage, so deduplication must not hash lifetime evidence.
    unique = []
    for effect in effects:
        if effect not in unique:
            unique.append(effect)
    return state, tuple(unique)
