"""Native battery authority and measurement records, with no economic selection."""
from __future__ import annotations
from dataclasses import dataclass, fields
from math import isfinite
from typing import Optional
if __package__:
    from .battery_conversion import Conversion
else:
    from battery_conversion import Conversion
RESPONSE_MODEL = "pv-first-v1"
OPERATIONS = ("self_consumption", "solar_charge", "supply_house", "grid_charge", "export", "hold")

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
        if self.response_model_revision not in (RESPONSE_MODEL, "pv-first-dc-v2"):
            raise ValueError("unsupported native response model")



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
    stored_at_ms: Optional[int] = None

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
    conversion: Optional[Conversion] = None

    def __post_init__(self):
        if self.wear_basis not in ("ac_throughput", "discharged_storage"):
            raise ValueError("unknown battery wear basis")
        for field in fields(self):
            if field.name not in ("wear_basis", "conversion"):
                _num(getattr(self, field.name), 0, 1e6)
        if not self.cutoff_kwh < self.capacity_kwh:
            raise ValueError("invalid battery physical state bounds")
        if not 0 < self.charge_efficiency <= 1 or not 0 < self.discharge_efficiency <= 1:
            raise ValueError("invalid battery efficiencies")


