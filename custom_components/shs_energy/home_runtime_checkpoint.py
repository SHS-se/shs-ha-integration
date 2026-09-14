"""Closed version-1 JSON for the offline runtime and conservative crash restoration."""
from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from functools import lru_cache
import json
from math import isfinite
import types
from typing import get_args, get_origin, get_type_hints, Literal, Union

if __package__:
    from . import home_runtime as runtime
else:
    import home_runtime as runtime

MAX_BYTES = 1_000_000


def _encode(value):
    if is_dataclass(value):
        return {"type": type(value).__name__, **{field.name: _encode(getattr(value, field.name)) for field in fields(value)}}
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    return value


@lru_cache(maxsize=64)
def _hints(cls):
    return get_type_hints(cls)


def _decode(value, expected):
    origin, args = get_origin(expected), get_args(expected)
    if origin in (Union, types.UnionType):
        candidates = [kind for kind in args if is_dataclass(kind) and isinstance(value, dict) and value.get("type") == kind.__name__]
        if candidates:
            return _decode(value, candidates[0])
        for kind in args:
            if not is_dataclass(kind):
                try:
                    return _decode(value, kind)
                except ValueError:
                    pass
        raise ValueError("unsupported tagged value or union member")
    if origin is Literal:
        if value not in args or not any(type(value) is type(item) for item in args):
            raise ValueError("unsupported literal")
        return value
    if expected is type(None):
        if value is not None:
            raise ValueError("expected null")
        return None
    if origin is tuple:
        if type(value) is not list or len(value) > 4096:
            raise ValueError("expected a bounded array")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(item, args[0]) for item in value)
        if len(value) != len(args):
            raise ValueError("tuple length differs")
        return tuple(_decode(item, kind) for item, kind in zip(value, args))
    if is_dataclass(expected):
        names = {field.name for field in fields(expected)}
        if type(value) is not dict or set(value) != names | {"type"} or value["type"] != expected.__name__:
            raise ValueError("unknown/missing fields or incompatible record type")
        annotations = _hints(expected)
        return expected(**{name: _decode(value[name], annotations[name]) for name in names})
    if expected is int:
        if type(value) is not int or abs(value) > 2 ** 53:
            raise ValueError("expected bounded integer")
    elif expected is float:
        if type(value) not in (int, float) or not isfinite(value):
            raise ValueError("expected finite number")
    elif expected in (str, bool):
        if type(value) is not expected or (expected is str and len(value) > 1024):
            raise ValueError("invalid scalar")
    else:
        raise ValueError("unsupported domain type")
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def read_runtime_json(data):
    """Read bounded JSON without duplicate keys or nonfinite constants."""
    if not isinstance(data, (str, bytes)) or len(data if isinstance(data, bytes) else data.encode()) > MAX_BYTES:
        raise ValueError("runtime JSON exceeds byte limit")
    return json.loads(data, object_pairs_hook=_unique, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"nonfinite JSON: {value}")))


def _check_state(state):
    if state.resume_after_ms > state.last_time_ms:
        raise ValueError("resume fence exceeds journal time")
    for group in state.groups:
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
                if (step.key not in group.spec.control_keys or set(dict(step.before)) != set(group.spec.control_keys)
                        or not runtime._within(step.possible, group.spec.maximum)
                        or (index and not runtime._same(plan.steps[index - 1].after, step.before))):
                    raise ValueError("invalid sequence control surface")
            if not runtime._same(plan.steps[-1].after, request.target):
                raise ValueError("sequence does not reach request")
        for attempt in group.attempts:
            if attempt.stage == "prepared" and (group.plan is None or attempt.generation != group.plan.generation
                    or attempt.step_index != group.plan.index or group.plan.index >= len(group.plan.steps)
                    or attempt.step != group.plan.steps[group.plan.index]
                    or (attempt.request_id, attempt.request_revision, attempt.purpose) !=
                       (group.plan.request_id, group.plan.request_revision, group.plan.purpose)):
                raise ValueError("preparation has no matching sequence")
    return state


def encode_checkpoint(state: runtime.HomeState) -> bytes:
    _check_state(state)
    data = json.dumps({"schema_version": 1, "state": _encode(state)}, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > MAX_BYTES:
        raise ValueError("checkpoint exceeds byte limit")
    return data


def decode_checkpoint(data: bytes) -> runtime.HomeState:
    value = read_runtime_json(data)
    if type(value) is not dict or set(value) != {"schema_version", "state"} or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported checkpoint version/fields")
    return _check_state(_decode(value["state"], runtime.HomeState))


def restore_checkpoint(data: bytes, now_ms: int):
    """No replayed sends: require fresh authority/frame/readback after restart."""
    old = decode_checkpoint(data)
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("invalid resume time")
    after = max(old.last_time_ms, now_ms)
    groups = tuple(replace(group, authority_confirmed=False, observation=None, plan=None,
                           attempts=tuple(replace(a, stage="ambiguous") for a in group.attempts),
                           owned=group.owned or bool(group.attempts), waiting_for=None, status="resuming")
                   for group in old.groups)
    state = replace(old, revision=old.revision + 1, last_time_ms=after, resume_after_ms=after, groups=groups, frame=None)
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
