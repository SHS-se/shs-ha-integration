"""App-owned device choices, independent of participation and entity mappings."""

from typing import TypedDict

from .const import OPT_DEVICE_CATEGORY_OVERRIDES
from .device_controls import CONTROL_TYPES

DEVICE_SETUPS = "device_setups"
LOAD_TYPES = ("fixed_full_load", "variable_full_load", "duty_cycle", "inverter")


class DeviceSetup(TypedDict):
    control_type: str | None
    load_type: str
    revision: int


def read_device_setups(rows: object) -> dict[str, DeviceSetup]:
    """Import the website's active and retained choices as one validated set."""
    if not isinstance(rows, list):
        raise ValueError("The website did not return the existing device settings")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or "control_type" not in row:
            raise ValueError("The website returned invalid device settings")
        key = row.get("key")
        method, load, revision = row.get("control_type"), row.get("load_type"), row.get("revision")
        if (not isinstance(key, str) or not key or key in result
                or (method is not None and method not in CONTROL_TYPES)
                or load not in LOAD_TYPES or type(revision) is not int or revision < 1):
            raise ValueError("The website returned invalid device settings")
        result[key] = DeviceSetup(control_type=method, load_type=load, revision=revision)
    return result


def with_device_setup(device: dict, options: dict) -> dict:
    key = device["key"]
    settings = {}
    if setup := options.get(DEVICE_SETUPS, {}).get(key):
        settings.update(control_type=setup["control_type"], load_type=setup["load_type"])
    if category := options.get(OPT_DEVICE_CATEGORY_OVERRIDES, {}).get(key):
        settings["category"] = category
    return {**device, **settings}
