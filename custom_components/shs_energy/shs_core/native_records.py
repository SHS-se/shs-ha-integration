"""Native command records shared with HA; independent of the policy reducer."""
from __future__ import annotations
from dataclasses import dataclass
from math import isfinite
from typing import Literal, Optional, Union
from .battery_physical import BatteryOperation, OPERATION_CEILINGS

Value = Union[str, float, int]
Controls = tuple[tuple[str, Value], ...]
Measurements = tuple[tuple[str, float], ...]
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

def _reason(value):
    if type(value) is not str or not 0 < len(value) <= 2000:
        raise ValueError("failure reason must be a nonempty string of at most 2000 characters")

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
class OperationBinding:
    operation: BatteryOperation
    target: Controls
    response_model_revision: str
    response_evidence: str
    native_guards: tuple[Guard, ...] = ()

    def __post_init__(self):
        _pairs(self.target)
        _identity(self.response_evidence)
        if self.response_model_revision not in ("pv-first-v1", "pv-first-dc-v2"):
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
        # Only a deliberate decision to let surplus reach the grid closes the
        # charge permission, and only the inert mode expresses it.
        modes = {"self_consumption": "Maximum Self Consumption", "solar_charge": "Maximum Self Consumption",
                 "supply_house": "Maximum Self Consumption", "hold": "Maximum Self Consumption",
                 "grid_charge": "Command Charging (PV First)",
                 "export": "Command Discharging (PV First)", "idle": "Standby"}
        for binding in self.bindings:
            op, target = binding.operation, dict(binding.target)
            if op.operation not in modes or set(target) != keys or target[self.mode_key] != modes[op.operation] or target[self.mode_key] not in self.mode_options:
                raise ValueError("unsupported native operation mapping")
            cc, dc = op.charge_limit_w, op.discharge_limit_w
            expected = (self.charge_max_w, self.discharge_max_w) if op.operation == "self_consumption" else (cc, dc)
            if op.operation == "self_consumption" and (cc, dc) != expected:
                raise ValueError("self consumption must score the actual rated ceilings")
            required = OPERATION_CEILINGS.get(op.operation)
            if required and any(bool(value > 0) is not needed for value, needed in zip((cc, dc), required)):
                raise ValueError("native source semantics differ")
            for key, power, maximum in zip((self.charge_key, self.discharge_key), expected,
                                            (self.charge_max_w, self.discharge_max_w)):
                _number(target[key], "native ceiling")
                if target[key] != power or power > maximum or abs(power / self.quantum_w - round(power / self.quantum_w)) > 1e-9:
                    raise ValueError("native target differs from exact quantum/capability")

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
    relief_rules: tuple[ReliefRule, ...] = ()

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
class NeedTransition:
    group_id: str
    generation: int
    purpose: Purpose
    request: Request
    observation: Observation
    adapter_revision: str
    token: int
    deadline_ms: int
    command_controls: Controls

def _within(candidate, maximum):
    return candidate.import_w <= maximum.import_w and candidate.export_w <= maximum.export_w
