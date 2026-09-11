"""Sensor-or-number equipment quantities in the planner's canonical units."""
from math import isfinite

# Persist numbers in kWh, W and SOC fractions; sensor units are explicit.
QUANTITY_UNITS = {"kWh": {"kWh": 1, "Wh": 0.001, "MWh": 1000},
                  "W": {"W": 1, "kW": 1000}, "%": {"%": 0.01}}
BATTERY_QUANTITIES = {
    "battery_capacity_kwh": ("kWh", 0.1, None),
    "battery_charge_max_w": ("W", 1, None),
    "battery_discharge_max_w": ("W", 1, None),
    "battery_min_soc": ("%", 0, 1),
}


def resolve_quantity(value, read_entity, *, unit, minimum=None, maximum=None, label="Value"):
    """Read a selected sensor or a literal; an invalid sensor fails explicitly."""
    factor = 1
    if isinstance(value, str) and value.strip().startswith("sensor."):
        entity = value.strip()
        payload = read_entity(entity)
        if payload is None:
            raise ValueError(f"{label}: {entity} does not exist")
        sensor_unit = payload["attributes"].get("unit_of_measurement")
        if sensor_unit not in QUANTITY_UNITS[unit]:
            raise ValueError(f"{label}: {entity} must use {' or '.join(QUANTITY_UNITS[unit])}")
        factor = QUANTITY_UNITS[unit][sensor_unit]
        value = payload["state"]
    try:
        if isinstance(value, bool):
            raise ValueError()
        number = float(value) * factor
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}: choose a numeric sensor or enter a number") from error
    if not isfinite(number):
        raise ValueError(f"{label}: value must be finite")
    if minimum is not None and number < minimum:
        raise ValueError(f"{label}: value is below the allowed minimum")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label}: value exceeds the allowed maximum")
    return number


def resolve_battery_quantities(options, read_entity):
    """Resolve the same physical bounds for planning and local execution."""
    result = {}
    for key, (unit, minimum, maximum) in BATTERY_QUANTITIES.items():
        result[key] = resolve_quantity(options[key], read_entity, unit=unit,
                                       minimum=minimum, maximum=maximum, label=key)
    if result["battery_min_soc"] >= float(options["battery_max_soc"]):
        raise ValueError("Minimum charge must be below Maximum charge")
    return result
