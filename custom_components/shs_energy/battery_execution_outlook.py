"""Read-only explanation of the selected finite continuation, separate from control."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class Quarter:
    start_ms: int
    end_ms: int
    consumption_w: float
    solar_w: float
    import_sek_per_kwh: float


@dataclass(frozen=True)
class Event(Quarter):
    battery_w: float


@dataclass(frozen=True)
class Witness:
    kind: str
    anchor_kwh: float | None = None
    charge: Event | None = None
    discharge: Event | None = None


@dataclass(frozen=True)
class ExecutionOutlook:
    source_hash: str
    family_id: str
    battery_power_basis: str
    horizon_end_ms: int
    bridge: Quarter | None
    witnesses: Mapping[str, Witness]


def _fields(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise ValueError("invalid battery outlook fields")


def _number(value, minimum=None):
    if type(value) not in (int, float) or not isfinite(value) or (minimum is not None and value < minimum):
        raise ValueError("invalid battery outlook number")
    return value


def read_execution_outlook(value, policy):
    """Bind metadata to exactly the executable family; never grant control."""
    _fields(value, 'schema source_hash family_id battery_power_basis horizon_end_ms bridge witnesses')
    s = policy.summary
    basis = 'dc' if s.plant.conversion else 'ac'
    if (value['schema'] != 'battery-execution-outlook-v1' or
            value['source_hash'] != s.quality.source_hash or value['family_id'] != s.quality.family_id or
            value['battery_power_basis'] != basis):
        raise ValueError("battery outlook policy mismatch")
    horizon = _number(value['horizon_end_ms'], s.boundary_ms)
    if type(horizon) is not int or horizon % 900000:
        raise ValueError("invalid battery outlook horizon")

    def quarter(raw, event=False):
        _fields(raw, 'start_ms end_ms consumption_w solar_w import_sek_per_kwh' + (' battery_w' if event else ''))
        start, end = raw['start_ms'], raw['end_ms']
        if (type(start) is not int or type(end) is not int or start % 900000 or
                start < s.boundary_ms or end != start + 900000 or end > horizon):
            raise ValueError("invalid battery outlook quarter")
        _number(raw['consumption_w'], 0)
        _number(raw['solar_w'], 0)
        _number(raw['import_sek_per_kwh'])
        if event:
            if start <= s.boundary_ms or _number(raw['battery_w'], 0) == 0:
                raise ValueError("invalid battery outlook suffix event")
            return Event(**raw)
        return Quarter(**raw)

    bridge = quarter(value['bridge']) if value['bridge'] is not None else None
    if (horizon > s.boundary_ms) != (bridge is not None) or (bridge and bridge.start_ms != s.boundary_ms):
        raise ValueError("invalid battery outlook bridge")
    raw_witnesses = value['witnesses']
    if not isinstance(raw_witnesses, dict) or set(raw_witnesses) != {c.witness_id for c in s.cells}:
        raise ValueError("battery outlook witnesses mismatch")
    witnesses = {}
    for key, raw in raw_witnesses.items():
        if not isinstance(raw, dict):
            raise ValueError("invalid battery outlook witness")
        kind = raw.get('kind')
        if kind in ('idle', 'terminal'):
            _fields(raw, 'kind')
            if key != kind or (kind == 'terminal') != (bridge is None):
                raise ValueError("invalid stationary outlook")
            witnesses[key] = Witness(kind)
        elif kind == 'anchor':
            _fields(raw, 'kind anchor_kwh first_suffix_charge first_suffix_discharge')
            anchor = _number(raw['anchor_kwh'], s.plant.cutoff_kwh)
            if bridge is None or anchor > s.plant.capacity_kwh:
                raise ValueError("invalid battery outlook anchor")
            witnesses[key] = Witness(kind, anchor,
                quarter(raw['first_suffix_charge'], True) if raw['first_suffix_charge'] is not None else None,
                quarter(raw['first_suffix_discharge'], True) if raw['first_suffix_discharge'] is not None else None)
        else:
            raise ValueError("invalid battery outlook witness kind")
    return ExecutionOutlook(value['source_hash'], value['family_id'], basis, horizon, bridge, MappingProxyType(witnesses))


def describe_outlook(outlook, state, now_ms):
    """Use the applied choice, including hysteresis, and existing runtime freshness."""
    unavailable = {'state': 'unavailable'}
    if outlook is None or state is None or state.policy is None:
        return unavailable
    session, conditions = state.policy, state.conditions
    s, decision = session.compiled.summary, session.decision
    if (session.status not in ('active', 'diagnostic_only') or decision is None or
            not s.from_ms <= now_ms < min(s.until_ms, decision.valid_until_ms) or
            conditions is None or not conditions.at_ms <= now_ms < conditions.valid_until_ms or
            conditions.identity != s.identity.context or conditions.permissions != s.permissions or
            outlook.source_hash != s.quality.source_hash or outlook.family_id != s.quality.family_id):
        return unavailable
    selected = next((r for r in decision.ranked if r.operation.id == session.selected_id), None)
    if selected is None or selected.witness_id not in outlook.witnesses:
        return unavailable
    witness = outlook.witnesses[selected.witness_id]
    charge, discharge = witness.charge, witness.discharge
    if witness.kind == 'anchor':
        delta = witness.anchor_kwh - selected.energy_end_kwh
        # The compiler's bridge is exactly one quarter. DC energy is storage-side;
        # AC policies account for charge/discharge efficiency in the same rates.
        plant = s.plant
        if delta > 0:
            power = delta * 4000 / (1 if plant.conversion else plant.charge_efficiency)
            charge = Event(**asdict(outlook.bridge), battery_w=power)
        elif delta < 0:
            power = -delta * 4000 * (1 if plant.conversion else plant.discharge_efficiency)
            discharge = Event(**asdict(outlook.bridge), battery_w=power)
    hold = next((r for r in decision.ranked if r.operation.operation == 'hold'), None)
    return {
        'state': 'available', 'basis': 'verification' if session.status == 'diagnostic_only' else 'controlling',
        'selected_operation': selected.operation.id, 'evaluated_at_ms': decision.evaluated_at_ms,
        'horizon_end_ms': outlook.horizon_end_ms, 'battery_power_basis': outlook.battery_power_basis,
        'events': sorted([{'kind': kind, **asdict(event)} for kind, event in
                          (('charge', charge), ('discharge', discharge)) if event], key=lambda e: e['start_ms']),
        # Both alternatives include their own optimized continuation and terminal value.
        'benefit_vs_hold_sek': hold.full[-1] - selected.full[-1] if hold else None,
    }
