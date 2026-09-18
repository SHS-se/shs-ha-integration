"""Closed version-9 JSON for the offline runtime and conservative crash restoration."""
from __future__ import annotations

from dataclasses import replace
import json

if __package__:
    from . import home_runtime as runtime
    from .runtime_json import MAX_BYTES, encode_value as _encode, decode_value as _decode, read_runtime_json
else:
    import home_runtime as runtime
    from runtime_json import MAX_BYTES, encode_value as _encode, decode_value as _decode, read_runtime_json


def _check_state(state):
    if state.resume_after_ms > state.last_time_ms:
        raise ValueError("resume fence exceeds journal time")
    if state.ledger and any(stream.samples and stream.samples[-1].at_ms > state.last_time_ms for stream in state.ledger.streams):
        raise ValueError("meter evidence exceeds checkpoint time")
    if state.authority:
        runtime._validate_authority(state, state.authority)
    session = state.execution
    if session.account.contract:
        group = runtime._battery_group(state)
        if group is None:
            raise ValueError("execution account needs its physical battery group")
        if group.desired and group.desired.id != session.request_id:
            raise ValueError("battery request differs from its execution account")
        if session.status == "active":
            runtime._validate_execution(state, session.account.contract)
            if group.mode != "controlling" or group.desired is None or session.assessment is None:
                raise ValueError("active execution needs a request and assessment")
    for group in state.groups:
        if group.grant and group.grant.epoch != group.grant_epoch:
            raise ValueError("writer grant epoch differs from its fence")
        if group.grant_confirmed and group.grant is None:
            raise ValueError("grant confirmation needs an explicit grant")
        work = group.transition_work
        if isinstance(work, runtime.TransitionJob):
            if work.token >= group.next_transition or work.key.generation != group.generation:
                raise ValueError("transition job identity disagrees with checkpoint")
        if work is not None and (work.key.generation > group.generation
                                 or work.key.observed_revision > group.observation_revision):
            raise ValueError("transition work exceeds its observation/generation watermark")
        if group.plan is not None and work is not None:
            raise ValueError("transition work cannot coexist with an accepted sequence")
        if group.observation and group.observation.revision != group.observation_revision:
            raise ValueError("observation watermark disagrees")
        if group.observation and (set(dict(group.observation.controls)) != set(group.spec.control_keys)
                                  or not runtime._within(group.observation.envelope, group.spec.maximum)):
            raise ValueError("observation exceeds group scope")
        for request in (group.desired, group.release):
            if request is not None:
                runtime._validate_target(group, request)
        for attempt in group.attempts:
            prefix = f"{group.spec.id}:"
            suffix = attempt.id.removeprefix(prefix)
            if (not attempt.id.startswith(prefix) or not suffix.isascii() or not suffix.isdecimal()
                    or not 0 < int(suffix) < group.next_attempt):
                raise ValueError("attempt identity disagrees with persisted counter")
            if (attempt.prepared_revision > state.revision or attempt.generation > group.generation
                    or attempt.step.key not in group.spec.control_keys
                    or set(dict(attempt.step.before)) != set(group.spec.control_keys)
                    or attempt.observed_revision > group.observation_revision
                    or attempt.latest_effect_ms != attempt.send_by_ms + attempt.step.latest_effect_delay_ms):
                raise ValueError("checkpoint contains an impossible preparation")
            if attempt.grant.epoch > group.grant_epoch or attempt.send_by_ms > attempt.grant.expires_at_ms:
                raise ValueError("attempt exceeds its writer grant")
            if attempt.stage == "prepared" and attempt.grant != group.grant:
                raise ValueError("prepared attempt has a superseded grant")
            runtime._relief_rule(group, attempt.step)
            if not runtime._within(attempt.step.possible, group.spec.maximum):
                raise ValueError("attempt exceeds its declared scope")
        if group.plan and group.plan.generation != group.generation:
            raise ValueError("stale sequence in checkpoint")
        if group.plan:
            plan = group.plan
            request = group.desired if plan.purpose == "optimisation" else group.release
            if request is None or (request.id, request.revision) != (plan.request_id, plan.request_revision):
                raise ValueError("sequence does not name its request")
            for index, step in enumerate(plan.steps):
                runtime._relief_rule(group, step)
                if (step.key not in group.spec.control_keys or set(dict(step.before)) != set(group.spec.control_keys)
                        or not runtime._within(step.possible, group.spec.maximum)
                        or (index and not runtime._same(plan.steps[index - 1].after, step.before))):
                    raise ValueError("invalid sequence control surface")
            if not runtime._same(plan.steps[-1].after, request.target):
                raise ValueError("sequence does not reach request")
        for attempt in group.attempts:
            if attempt.stage == "prepared" and not runtime.preparation_matches(group.plan, attempt):
                raise ValueError("preparation has no matching sequence")
    return state


def encode_checkpoint(state: runtime.HomeState) -> bytes:
    _check_state(state)
    data = json.dumps({"schema_version": 9, "state": _encode(state)}, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > MAX_BYTES:
        raise ValueError("checkpoint exceeds byte limit")
    return data


def decode_checkpoint(data: bytes) -> runtime.HomeState:
    value = read_runtime_json(data)
    if type(value) is not dict or set(value) != {"schema_version", "state"} or type(value["schema_version"]) is not int:
        raise ValueError("unsupported checkpoint version/fields")
    if value["schema_version"] == 7:
        # One-way retirement of economic authority. Keep issued native effects,
        # their writer fence and approved release; never resume an old selector.
        raw = value["state"]
        if not isinstance(raw, dict) or "policy" not in raw:
            raise ValueError("invalid retired controller journal")
        retired = raw.pop("policy")
        raw["execution"] = _encode(runtime.ExecutionSession())
        if raw.get("conditions"):
            raw["conditions"]["stored_at_ms"] = None
        for group in raw["groups"] if retired is not None else ():
            issued = [a for a in group["attempts"] if a["stage"] != "prepared"]
            group.update(desired=None, plan=None, transition_work=None, attempts=issued,
                generation=group["generation"] + 1,
                release_pending=group["release_pending"] or group["owned"] or bool(issued))
        return _check_state(_decode(raw, runtime.HomeState))
    if value["schema_version"] == 8:
        value["state"]["execution"] = upgrade_execution_session(value["state"]["execution"])
    elif value["schema_version"] != 9:
        raise ValueError("unsupported checkpoint version/fields")
    return _check_state(_decode(value["state"], runtime.HomeState))


def upgrade_execution_session(raw):
    """One-way upgrade of the exact v8 session shape, including archived roots."""
    from dataclasses import fields
    old_fields = {'type', *(f.name for f in fields(runtime.ExecutionSession))} - {'plan_rejection'}
    if isinstance(raw, dict) and set(raw) == old_fields and raw.get('type') == 'ExecutionSession':
        return {**raw, 'plan_rejection': None}
    return raw


def restore_checkpoint(data: bytes, now_ms: int):
    """No replayed sends: require fresh authority/frame/readback after restart."""
    return restore_state(decode_checkpoint(data), now_ms)


def restore_state(old, now_ms):
    _check_state(old)
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("invalid resume time")
    after = max(old.last_time_ms, now_ms)
    groups = tuple(replace(group, grant_confirmed=False, observation=None, plan=None,
                           attempts=tuple(replace(a, stage="ambiguous") for a in group.attempts),
                           owned=group.owned or bool(group.attempts),
                           transition_work=runtime.resumed_transition_work(group, after, old.limits), status="resuming")
                   for group in old.groups)
    state = replace(old, revision=old.revision + 1, last_time_ms=after, resume_after_ms=after, groups=groups, frame=None)
    state = replace(state, conditions=None,
                    execution=replace(state.execution, assessment=None, status="awaiting_context"))
    # Keep the current desired operation; fresh context/grant/readback may adopt it
    # directly. Expiry and a pending passive-mode release remain authoritative.
    effects = (runtime.Persist(state), *(runtime.ConfirmAuthority(g.spec.id) for g in groups),
               *(runtime.Observe(g.spec.id) for g in groups))
    return state, effects


def decode_event(value) -> runtime.Event:
    """Trace-only event input; production adapters will have separate readers."""
    return _decode(value, runtime.Event)


def event_json(event: runtime.Event):
    return _encode(event)


def effects_json(effects):
    return [_encode(effect) for effect in effects]
