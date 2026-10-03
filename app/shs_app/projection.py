"""HA's display plan omits per-slot planner diagnostic trees, not plan facts."""
from copy import deepcopy


def display_plan(plan):
    if plan is None:
        return None
    result = {key: deepcopy(value) for key, value in plan.items() if key not in ('plans', 'execution_plan')}
    if 'plans' in plan:
        result['plans'] = {
            key: {**{field: deepcopy(value) for field, value in scenario.items() if field != 'slots'},
                  **({'slots': [{field: deepcopy(value) for field, value in slot.items() if field != 'decision'}
                                for slot in scenario['slots']]} if 'slots' in scenario else {})}
            for key, scenario in plan['plans'].items()}
    if 'execution_plan' in plan:
        execution = plan['execution_plan']
        result['execution_plan'] = display_plan(execution) if isinstance(execution, dict) else deepcopy(execution)
    return result


class DisplayPlan:
    """One read-only projection per immutable plan object, independent of status."""
    def __init__(self):
        self.source = None
        self.value = None

    def get(self, plan):
        if plan is not self.source:
            self.value = display_plan(plan)
            self.source = plan
        return self.value
