"""Exact native transitions admitted by installation commissioning evidence."""
from collections import deque
from dataclasses import dataclass

if __package__:
    from .home_runtime import NativeCatalog, NeedTransition, Proposed, Step, ReliefRule
else:
    from home_runtime import NativeCatalog, NeedTransition, Proposed, Step, ReliefRule


@dataclass(frozen=True)
class CommissionedAdapter:
    catalog: NativeCatalog
    evidence_id: str
    steps: tuple[Step, ...]
    observed_surface_revision: str
    sampled_scope_enforcement: bool

    def __post_init__(self):
        if (not self.evidence_id or self.evidence_id.startswith("synthetic")
                or self.observed_surface_revision != self.catalog.control_surface_revision):
            raise ValueError("matching physical commissioning evidence is required")
        if type(self.sampled_scope_enforcement) is not bool or not 0 < len(self.steps) <= 512:
            raise ValueError("commissioned scope capability and bounded transitions are required")
        keys = {self.catalog.mode_key, self.catalog.charge_key, self.catalog.discharge_key}
        edges = set()
        for step in self.steps:
            edge = (tuple(sorted(step.before)), tuple(sorted(step.after)))
            if (set(dict(step.before)) != keys or step.key not in keys or edge in edges
                    or step.evidence != self.evidence_id):
                raise ValueError("transition differs from commissioned surfaces/evidence")
            edges.add(edge)
            for controls in (dict(step.before), dict(step.after)):
                if controls[self.catalog.mode_key] not in self.catalog.mode_options:
                    raise ValueError("transition uses an unknown mode")
                for key, maximum in ((self.catalog.charge_key, self.catalog.charge_max_w),
                                     (self.catalog.discharge_key, self.catalog.discharge_max_w)):
                    watts = controls[key]
                    if (type(watts) not in (float, int) or not 0 <= watts <= maximum or
                            abs(watts / self.catalog.quantum_w - round(watts / self.catalog.quantum_w)) > 1e-9):
                        raise ValueError("transition exceeds commissioned native resolution/rating")

    def propose(self, effect: NeedTransition):
        if effect.adapter_revision != self.catalog.adapter_revision:
            raise ValueError("adapter identity changed")
        before = tuple(sorted(effect.observation.controls))
        target = tuple(sorted(effect.request.target))
        # Only measured transitions can form a route. Never infer an order from
        # service names, numerical direction, or a previously successful target.
        edges = {}
        for step in self.steps:
            edges.setdefault(tuple(sorted(step.before)), []).append(step)
        queue = deque([(before, ())])
        visited = {before}
        while queue:
            controls, route = queue.popleft()
            if controls == target:
                return Proposed(effect.group_id, effect.generation, effect.request.id,
                    effect.request.revision, effect.observation.revision,
                    effect.adapter_revision, route, effect.token)
            if len(route) >= 32:
                continue
            for step in edges.get(controls, ()):
                after = tuple(sorted(step.after))
                if after not in visited:
                    visited.add(after)
                    queue.append((after, (*route, step)))
        raise ValueError("no commissioned transition from observed controls to requested target")


@dataclass(frozen=True)
class SigenAdapter:
    """PV First ESS register protocol; ceilings are battery-terminal DC watts.

    The installation supplies the number/select metadata. Numerical targets do
    not need individual commissioning records. Each assignment is journalled and
    confirmed from new physical reports by HomeHost before the next assignment.
    """
    catalog: NativeCatalog
    conversion: object

    def envelope(self, controls):
        if __package__:
            from .home_runtime import Envelope
        else:
            from home_runtime import Envelope
        values = dict(controls)
        mode = values[self.catalog.mode_key]
        return Envelope(self.conversion.grid_charge.input(min(values[self.catalog.charge_key],self.catalog.charge_max_w))
                        if mode == "Command Charging (PV First)" else 0,
                        self.conversion.discharge.output(min(values[self.catalog.discharge_key],self.catalog.discharge_max_w))
                        if mode == "Command Discharging (PV First)" else 0)

    def propose(self, effect: NeedTransition):
        if effect.adapter_revision != self.catalog.adapter_revision:
            raise ValueError("Sigen adapter revision changed")
        before, target = effect.observation.controls, dict(effect.request.target)
        c = self.catalog
        if set(target) != {c.mode_key,c.charge_key,c.discharge_key} or target[c.mode_key] not in c.mode_options:
            raise ValueError("unsupported Sigen target")
        for key,maximum in ((c.charge_key,c.charge_max_w),(c.discharge_key,c.discharge_max_w)):
            value=target[key]
            if type(value) not in (float,int) or not 0<=value<=maximum or abs(value/c.quantum_w-round(value/c.quantum_w))>1e-9:
                raise ValueError("Sigen target exceeds native rating or resolution")
        if target[c.mode_key] == "Command Charging (Grid First)":
            raise ValueError("Grid First is not supported")
        assignments=[]
        if dict(before)[c.mode_key] != target[c.mode_key]:
            assignments.extend(((c.charge_key,0),(c.discharge_key,0),(c.mode_key,target[c.mode_key])))
        assignments.extend(sorted(((c.charge_key,target[c.charge_key]),(c.discharge_key,target[c.discharge_key])),key=lambda row:row[1]))
        steps=[];relief=[]
        for key,value in assignments:
            if dict(before)[key] == value:
                continue
            after=tuple((k,value if k==key else v) for k,v in before)
            a,b=self.envelope(before),self.envelope(after)
            from dataclasses import replace
            possible=replace(a,import_w=max(a.import_w,b.import_w),export_w=max(a.export_w,b.export_w))
            relief_id=None
            # Protocol-specific proof: reducing an ESS ceiling in the same EMS
            # mode cannot increase battery import/export beyond its prior bound.
            if key!=c.mode_key and value<dict(before)[key] and b!=a and b.import_w<=a.import_w and b.export_w<=a.export_w:
                from hashlib import sha256
                relief_id="sigen-ceiling-"+sha256(repr((before,key,value)).encode()).hexdigest()[:24]
                relief.append(ReliefRule(relief_id,before,after,a,possible,b,effect.request.native_guards,"sigen-modbus-v2.9-ess-pv-first"))
            step=Step(key,value,before,effect.request.native_guards,possible,75000,75000,True,"sigen-modbus-v2.9-ess-pv-first",relief_id)
            steps.append(step);before=step.after
        return Proposed(effect.group_id,effect.generation,effect.request.id,effect.request.revision,
                        effect.observation.revision,effect.adapter_revision,tuple(steps),effect.token,tuple(relief))
