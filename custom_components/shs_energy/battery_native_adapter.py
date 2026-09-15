"""Exact native transitions admitted by installation commissioning evidence."""
from collections import deque
from dataclasses import dataclass

if __package__:
    from .home_runtime import NativeCatalog, NeedTransition, Proposed, Step
else:
    from home_runtime import NativeCatalog, NeedTransition, Proposed, Step


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
