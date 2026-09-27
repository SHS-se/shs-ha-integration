"""HA's display plan omits per-slot planner diagnostic trees, not plan facts."""
from copy import deepcopy


def display_plan(plan):
    if plan is None:
        return None
    result = deepcopy(plan)
    def compact(value):
        for scenario in value.get('plans', {}).values():
            for slot in scenario.get('slots', []):
                slot.pop('decision', None)
        execution = value.get('execution_plan')
        if isinstance(execution, dict):
            compact(execution)
    compact(result)
    return result
