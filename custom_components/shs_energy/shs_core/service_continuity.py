"""Recognize accepted instructions independently of an unavailable store projection."""

from math import isfinite


def _finite(value):
    return type(value) in (int, float) and isfinite(value)


def retained_service_instruction(plan, slot, device):
    """Uncovered or unreadable instructions hold settings instead of requesting OFF."""
    if not isinstance(plan, dict) or not isinstance(slot, dict) or device not in ("ev", "pool"):
        return False
    issues = [issue for issue in (plan.get("measurement_issues") or [])
              if isinstance(issue, dict) and issue.get("device") == device]
    if not issues:
        return False
    for issue in issues:
        value = issue.get("value")
        if value is None:
            continue
        if not isinstance(value, str):
            return False
        try:
            if isfinite(float(value)):
                return False
        except ValueError:
            pass
    models = [model for model in (plan.get("device_models") or [])
              if isinstance(model, dict) and model.get("planning_role") == "controllable"]
    if device == "pool":
        return (any(model.get("planning_service") == "pool" for model in models)
                and type(slot.get("pool_permitted")) is bool
                and _finite(slot.get("pool_command_w")) and slot["pool_command_w"] >= 0
                and _finite(plan.get("retained_pool_stop_temperature_c")))
    models = [model for model in models if model.get("category") == "ev_charging"]
    amps, minimum, maximum = (slot.get(key) for key in
                              ("ev_target_current_a", "ev_min_current_a", "ev_max_current_a"))
    if (not models or not all(_finite(value) for value in (amps, minimum, maximum))
            or not 0 <= minimum <= amps <= maximum):
        return False
    commands = slot.get("device_commands")
    if not isinstance(commands, dict):
        return False
    return all(model.get("control_type") == "variable_power"
               and isinstance(command := commands.get(model.get("key")), dict)
               and command.get("type") == "variable_power"
               and command.get("unit") == "A" and _finite(command.get("value"))
               and command["value"] == amps for model in models)
