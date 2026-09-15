"""Device operating authority. Missing choices always mean monitoring."""
from copy import deepcopy

MODES = ("monitoring", "planning", "control_verification", "controlling")
EXECUTING_MODES = {"control_verification", "controlling"}


def device_mode(options, device):
    key = "$" + device if device in ("battery", "pool", "ev") else device.removeprefix("device:")
    mode = options.get("device_modes", {}).get(key, "monitoring")
    if mode not in MODES:
        raise ValueError(f"Invalid operating mode for {key}: {mode}")
    return mode


def system_device_keys(devices, options):
    """The single card owning each physical pool/EV controller."""
    if __package__:
        from .device_controls import mapped_planning_path
    else:
        from device_controls import mapped_planning_path
    owners = {}
    for system in ("pool", "ev"):
        candidates = [device for device in devices if mapped_planning_path(
            device, device.get("mapping", options.get("device_control_mappings", {}).get(device["key"], {})),
            options.get("pool_water_temperature_entity")) == system]
        candidates.sort(key=lambda d: (d.get("control_type") != "setpoint", d["key"]))
        if candidates:
            owners[candidates[0]["key"]] = system
    return owners


def planning_devices(devices, options):
    """Keep monitored meters in base load, with their observations intact."""
    result = deepcopy(devices)
    owners = system_device_keys(devices, options)
    for device in result:
        owner = owners.get(device["key"], "device:" + device["key"])
        if device_mode(options, owner) == "monitoring":
            device.update(planning_role="base_load", control_type=None)
    return result


def ownership_configuration(options, device):
    """Unrelated mode choices do not release an existing device's ownership."""
    result = deepcopy(options)
    result["device_modes"] = {"owner": device_mode(options, device)}
    result.pop("planning_mode", None)
    result.pop("configuration_reviewed_at", None)
    for system in ("battery", "pool", "ev"):
        result.pop(system + "_control_enabled", None)
    return result


def operating_mode_identity(options):
    """Canonical coupled authority, including choices for temporarily absent devices."""
    configured = options.get("device_modes", {})
    if not isinstance(configured, dict) or any(mode not in MODES for mode in configured.values()):
        raise ValueError("Invalid device operating modes")
    return dict(sorted({"$battery": "monitoring", "$pool": "monitoring", "$ev": "monitoring",
                        **{key: mode for key, mode in configured.items() if mode != "monitoring"}}.items()))


def scoped_plan(plan, options, device):
    """A hypothetical schedule never acquires live authority through a mode change."""
    if not isinstance(plan, dict):
        return None
    scope = plan.get("operating_scope")
    if not isinstance(scope, dict) or scope.get("modes") != operating_mode_identity(options):
        return None
    if device_mode(options, device) == "controlling":
        execution = plan.get("execution_plan")
        return execution if isinstance(execution, dict) else None
    return plan
