"""Validate explicit battery intent at the planner and executor boundary."""
from math import isfinite

# Schema 3 separates the two questions a quarter answers. `hold` is a decision
# about spending stored energy and leaves the automatic mode free to absorb
# surplus; `idle` keeps the stored energy too, but deliberately lets the
# surplus reach the grid instead of storing it.
SCHEMA_VERSIONS = (2, 3)

BATTERY_MODE_KEYS = {
    "self_consumption": "battery_mode_baseline", "solar_charge": "battery_mode_baseline",
    "supply_house": "battery_mode_baseline", "hold": "battery_mode_baseline",
    "grid_charge": "battery_mode_charge", "export": "battery_mode_discharge",
    "idle": "battery_mode_idle",
}
# Schema 2 expressed both meanings of hold with the inert native mode.
LEGACY_BATTERY_MODE_KEYS = {**BATTERY_MODE_KEYS, "hold": "battery_mode_idle"}

SCHEMA_OPERATIONS = {2: {"self_consumption", "solar_charge", "grid_charge", "supply_house", "export", "hold"}}
SCHEMA_OPERATIONS[3] = SCHEMA_OPERATIONS[2] | {"idle"}
OPERATIONS = SCHEMA_OPERATIONS[3]
# True: the ceiling must be positive. False: it must be zero. Absent: unconstrained.
CEILINGS = {
    2: {"solar_charge": (True, False), "grid_charge": (True, False), "supply_house": (False, True),
        "export": (False, True), "hold": (False, False)},
    3: {"solar_charge": (True, False), "grid_charge": (True, False), "supply_house": (True, True),
        "export": (False, True), "hold": (True, False), "idle": (False, False)},
}
# A permission ceiling is the plant's own limit, so the forecast does not reach it.
PERMISSIONS = {2: {"self_consumption", "solar_charge", "supply_house"},
               3: {"self_consumption", "solar_charge", "supply_house", "hold", "idle"}}
FIELDS = {"schema_version", "operation", "charge_limit_w", "discharge_limit_w", "allow_grid_charge", "allow_battery_export"}


def battery_mode_key(command):
    """The configured native mode this operation selects, by command schema."""
    keys = BATTERY_MODE_KEYS if command["schema_version"] >= 3 else LEGACY_BATTERY_MODE_KEYS
    return keys[command["operation"]]


def validate_battery_command(slot):
    command = slot.get("battery_command")
    if not isinstance(command, dict) or set(command) != FIELDS or type(command.get("schema_version")) is not int or command["schema_version"] not in SCHEMA_VERSIONS:
        raise ValueError("a versioned battery operation with both power ceilings is required")
    schema = command["schema_version"]
    operation = command["operation"]
    if not isinstance(operation, str) or operation not in SCHEMA_OPERATIONS[schema]:
        raise ValueError("unsupported battery operation")
    for key in ("charge_limit_w", "discharge_limit_w"):
        value = command[key]
        if type(value) not in (int, float) or not isfinite(value) or value < 0:
            raise ValueError(f"battery {key} must be finite and non-negative")
    for key, expected in (("allow_grid_charge", operation == "grid_charge"), ("allow_battery_export", operation == "export")):
        if command[key] is not expected:
            raise ValueError(f"battery {key} contradicts its operation")
    charge, discharge = command["charge_limit_w"], command["discharge_limit_w"]
    for value, required, label in zip((charge, discharge), CEILINGS[schema].get(operation, (None, None)),
                                      ("charge", "discharge")):
        if required is None:
            continue
        if required and value <= 0:
            raise ValueError(f"{operation} requires a positive {label} ceiling")
        if not required and value != 0:
            raise ValueError(f"{operation} requires a zero {label} ceiling")
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
    # Native regulation follows actual surplus/deficit within the server's
    # permission. Forecast watts need not equal that permission's ceiling.
    if operation not in PERMISSIONS[schema] and (abs(flows[0] - charge) > .01 or abs(flows[1] - discharge) > .01):
        raise ValueError("battery allocation and operation ceilings disagree")
    return command
