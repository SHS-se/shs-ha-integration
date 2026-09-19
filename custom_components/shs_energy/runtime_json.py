"""Bounded closed JSON records shared by the offline runtime readers."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from functools import lru_cache
import json
from math import isfinite
import types
from typing import get_args, get_origin, get_type_hints, Literal, Union

MAX_BYTES = 1_000_000


def encode_value(value):
    if is_dataclass(value):
        return {"type": type(value).__name__, **{field.name: encode_value(getattr(value, field.name)) for field in fields(value)}}
    if isinstance(value, tuple):
        return [encode_value(item) for item in value]
    return value


class Records:
    """Immutable runtime records whose JSON is written later.

    Records never change once created, so a report can hold them while its
    mutable parts are serialized, and encode them afterwards in another thread.
    """
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


def record_json(value, dumps):
    """JSON fragments of `encode_value(value)`, encoding one sequence item at a time.

    `dumps` returns compact JSON bytes. Only one record's encoded copy exists
    at a time, however long the account or trace history is.
    """
    if isinstance(value, tuple):
        yield b"["
        for index, item in enumerate(value):
            yield (b"," if index else b"") + dumps(encode_value(item))
        yield b"]"
    elif is_dataclass(value) and not isinstance(value, type):
        yield b'{"type":' + dumps(type(value).__name__)
        for field in fields(value):
            yield b"," + dumps(field.name) + b":"
            yield from record_json(getattr(value, field.name), dumps)
        yield b"}"
    else:
        yield dumps(encode_value(value))


@lru_cache(maxsize=64)
def _hints(cls):
    return get_type_hints(cls)


def decode_value(value, expected):
    origin, args = get_origin(expected), get_args(expected)
    if origin in (Union, types.UnionType):
        candidates = [kind for kind in args if is_dataclass(kind) and isinstance(value, dict) and value.get("type") == kind.__name__]
        if candidates:
            return decode_value(value, candidates[0])
        for kind in args:
            if not is_dataclass(kind):
                try:
                    return decode_value(value, kind)
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
            return tuple(decode_value(item, args[0]) for item in value)
        if len(value) != len(args):
            raise ValueError("tuple length differs")
        return tuple(decode_value(item, kind) for item, kind in zip(value, args))
    if is_dataclass(expected):
        names = {field.name for field in fields(expected)}
        if type(value) is not dict or set(value) != names | {"type"} or value["type"] != expected.__name__:
            raise ValueError("unknown/missing fields or incompatible record type")
        annotations = _hints(expected)
        return expected(**{name: decode_value(value[name], annotations[name]) for name in names})
    if expected is int:
        if type(value) is not int or abs(value) > 2 ** 53:
            raise ValueError("expected bounded integer")
    elif expected is float:
        if type(value) not in (int, float) or not isfinite(value):
            raise ValueError("expected finite number")
    elif expected in (str, bool):
        if type(value) is not expected or (expected is str and len(value) > MAX_BYTES):
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
