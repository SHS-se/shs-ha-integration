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


def hot_water_setup_errors(mapping, *, field_errors=None):
    value = mapping.get("power")
    if isinstance(value, str) and value.startswith("sensor."):
        return []
    message = "Choose the hot-water heater's instantaneous power sensor to detect when heating finishes"
    if field_errors is not None:
        field_errors["power"] = [message]
    return [message]


class HotWaterConfigurationError(ValueError):
    def __init__(self, mapping, key):
        fields = {}
        super().__init__("; ".join(hot_water_setup_errors(mapping, field_errors=fields)))
        self.fix = {"kind": "fields", "fields": [
            {"key": field, "scope": "mapping", "device_key": key, "message": "; ".join(messages)} for field, messages in fields.items()]}
        self.next_step = "Open the highlighted device Power field and choose the heater's instantaneous W or kW sensor."
