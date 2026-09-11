"""Validate explicit battery intent at the planner and executor boundary."""
from math import isfinite

OPERATIONS = {"self_consumption", "solar_charge", "grid_charge", "supply_house", "export", "hold"}
FIELDS = {"schema_version", "operation", "charge_limit_w", "discharge_limit_w", "allow_grid_charge", "allow_battery_export"}


def validate_battery_command(slot):
    command = slot.get("battery_command")
    if not isinstance(command, dict) or set(command) != FIELDS or type(command.get("schema_version")) is not int or command["schema_version"] != 1:
        raise ValueError("a versioned battery operation with both power ceilings is required")
    operation = command["operation"]
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise ValueError("unsupported battery operation")
    for key in ("charge_limit_w", "discharge_limit_w"):
        value = command[key]
        if type(value) not in (int, float) or not isfinite(value) or value < 0:
            raise ValueError(f"battery {key} must be finite and non-negative")
    for key, expected in (("allow_grid_charge", operation == "grid_charge"), ("allow_battery_export", operation == "export")):
        if command[key] is not expected:
            raise ValueError(f"battery {key} contradicts its operation")
    charge, discharge = command["charge_limit_w"], command["discharge_limit_w"]
    if operation in ("solar_charge", "grid_charge") and (charge <= 0 or discharge != 0):
        raise ValueError("charging requires a positive charge ceiling and zero discharge ceiling")
    if operation in ("supply_house", "export") and (discharge <= 0 or charge != 0):
        raise ValueError("discharging requires a positive discharge ceiling and zero charge ceiling")
    if operation == "hold" and (charge != 0 or discharge != 0):
        raise ValueError("hold requires both ceilings to be zero")
    flows = []
    for field in ("battery_charge_w", "battery_discharge_w"):
        value = slot.get(field)
        if type(value) not in (int, float) or not isfinite(value) or value < 0:
            raise ValueError(f"{field} must be finite and non-negative")
        flows.append(value)
    if flows[0] and flows[1]:
        raise ValueError("simultaneous battery charge and discharge is invalid")
    if flows[0] > charge + .01 or flows[1] > discharge + .01:
        raise ValueError("battery forecast exceeds its commanded ceilings")
    if operation != "self_consumption" and (abs(flows[0] - charge) > .01 or abs(flows[1] - discharge) > .01):
        raise ValueError("battery allocation and operation ceilings disagree")
    return command
