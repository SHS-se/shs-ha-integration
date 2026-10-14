"""Apply planner-owned gross-house demand thresholds without replanning."""
from math import isfinite

DEMAND_COMMAND = "house_demand_permission"
HOUSE_POWER_FIELD = "house_consumption_power_entity"


def requires_house_power(device):
    return device.get("category") == "hot_water" and device.get("control_type") == "permit_inhibit"


def validate_demand_command(command):
    if set(command) != {"type", "expected_permitted", "allow_below_w", "inhibit_above_w"}:
        return False
    low, high = command["allow_below_w"], command["inhibit_above_w"]
    return (command["type"] == DEMAND_COMMAND and isinstance(command["expected_permitted"], bool)
            and all(isinstance(w, (int, float)) and not isinstance(w, bool) and isfinite(w) for w in (low, high))
            and 0 <= low < high)


def demand_permission(command, house_w, previously_permitted):
    """The deadband retains the observed switch permission, including after restart.

    Expected permission is only a forecast. It never replaces a missing reading
    or determines the first physical command.
    """
    if not validate_demand_command(command) or not isfinite(house_w) or house_w < 0:
        raise ValueError("Invalid hot-water demand policy or house-power reading")
    if house_w <= command["allow_below_w"]:
        return True
    if house_w > command["inhibit_above_w"]:
        return False
    return previously_permitted


def hot_water_setup_errors(options, *, field_errors=None):
    value = options.get(HOUSE_POWER_FIELD)
    if isinstance(value, str) and value.startswith("sensor."):
        return []
    message = "Choose an instantaneous house-consumption power sensor for hot-water control"
    if field_errors is not None:
        field_errors[HOUSE_POWER_FIELD] = [message]
    return [message]


class HotWaterConfigurationError(ValueError):
    def __init__(self, options):
        fields = {}
        super().__init__("; ".join(hot_water_setup_errors(options, field_errors=fields)))
        self.fix = {"kind": "fields", "fields": [
            {"key": key, "message": "; ".join(messages)} for key, messages in fields.items()]}
        self.next_step = "Open the highlighted Energy setting and choose gross house power in W or kW, excluding battery charging."
