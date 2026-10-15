"""Finish a measured heating run before applying a scheduled inhibition."""
from math import isfinite

FINISH_HEATING_W = 500


def is_hot_water(device):
    return device.get("category") == "hot_water" and device.get("control_type") == "permit_inhibit"


def heating_permission(planned, observed_on, power_w):
    if not isinstance(planned, bool) or not isinstance(observed_on, bool) or not isfinite(power_w) or power_w < 0:
        raise ValueError("Invalid hot-water permission or measured heater power")
    # At exactly 500 W retain permission; completion requires strictly less.
    return planned or observed_on and power_w >= FINISH_HEATING_W


def boiler_running_power(mapping):
    """Reviewed electrical rating; a live sensor is never equipment capacity."""
    value = mapping.get("running_power_w")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if isfinite(value) and value > 0 else None


def hot_water_setup_errors(mapping, *, field_errors=None):
    errors = {}
    value = mapping.get("power")
    if not isinstance(value, str) or not value.startswith("sensor."):
        errors["power"] = ["Choose the boiler's own instantaneous power sensor to detect when heating finishes"]
    if boiler_running_power(mapping) is None:
        errors["running_power_w"] = ["Enter the switched boiler's electrical running power in watts, excluding heat-pump preheating"]
    if field_errors is not None:
        field_errors.update(errors)
    return [message for messages in errors.values() for message in messages]


class HotWaterConfigurationError(ValueError):
    def __init__(self, mapping, key):
        fields = {}
        super().__init__("; ".join(hot_water_setup_errors(mapping, field_errors=fields)))
        self.fix = {"kind": "fields", "fields": [
            {"key": field, "scope": "mapping", "device_key": key, "message": "; ".join(messages)} for field, messages in fields.items()]}
        self.next_step = "Open the highlighted boiler fields; choose its instantaneous W or kW sensor and enter its electrical running power."
