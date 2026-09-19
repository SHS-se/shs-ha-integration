"""Website planning and local execution are independent participation axes."""
from copy import deepcopy

MODES = ("control_verification", "controlling")
# Existing plan/journal identities retain inactive and historical modes. This
# reader is also used for releasing already-issued effects after migration.
WIRE_MODES = ("monitoring", "planning", *MODES)
EXECUTING_MODES = {"control_verification", "controlling"}


def device_mode(options, device):
    key = "$" + device if device in ("battery", "pool", "ev") else device.removeprefix("device:")
    mode = options.get("device_modes", {}).get(key)
    if mode is None:
        # Internal absence of admission, never a selectable execution mode.
        return "monitoring"
    if mode not in WIRE_MODES:
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
    """Website roles alone decide which Included meters are Planned."""
    excluded = set(options.get("excluded_device_readings", []))
    return deepcopy([device for device in devices if device["key"] not in excluded])


def ownership_configuration(options, device):
    """Unrelated mode choices do not release an existing device's ownership."""
    result = deepcopy(options)
    result["device_modes"] = {"owner": device_mode(options, device)}
    result.pop("planning_mode", None)
    result.pop("configuration_reviewed_at", None)
    for system in ("battery", "pool", "ev"):
        result.pop(system + "_control_enabled", None)
    return result


def operating_mode_identity(options, model_owners=()):
    """Include modeled monitoring owners and all explicit execution choices."""
    configured = options.get("device_modes", {})
    if not isinstance(configured, dict) or any(mode not in WIRE_MODES for mode in configured.values()):
        raise ValueError("Invalid device operating modes")
    return dict(sorted({**{owner: "monitoring" for owner in model_owners},
                        "$battery": "monitoring", "$pool": "monitoring", "$ev": "monitoring",
                        **{key: mode for key, mode in configured.items() if mode != "monitoring"}}.items()))


def scoped_plan(plan, options, device):
    """Keep the device's retained forecast branch; current permission owns writes.

    A mode change requests better forecasts, but cannot revoke existing ones.
    Explicitly granting control can execute the schedule previously verified.
    """
    if not isinstance(plan, dict):
        return None
    scope = plan.get("operating_scope")
    if not isinstance(scope, dict):
        return None
    if device_mode({"device_modes": scope["modes"]}, device) == "controlling":
        execution = plan.get("execution_plan")
        return execution if isinstance(execution, dict) else None
    return plan


def reconcile_admissions(options, devices, home):
    """Bind local grants to one acknowledged website admission per physical owner.

    A changed member, role revision, exclusion or planning method starts a new
    admission. Removing the saved grant also fences commands queued for the old
    options before a release is attempted by the single existing writer.
    """
    result = deepcopy(options)
    excluded = set(options.get("excluded_device_readings", []))
    owners = system_device_keys(devices, options)
    groups = {}
    for device in devices:
        if device["key"] in excluded or device.get("planning_role") != "controllable":
            continue
        owner = "$" + owners[device["key"]] if device["key"] in owners else device["key"]
        groups.setdefault(owner, []).append([device["key"], device.get("planning_choice_at"), device.get("control_type")])
    battery = home.get("battery", {})
    if options.get("battery_enabled", True) and "$battery" not in excluded and battery.get("included") is True:
        groups["$battery"] = [["$battery", battery.get("choice_at"), "battery"]]
    groups = {key: sorted(members) for key, members in sorted(groups.items())}
    previous = options.get("planning_admissions", {})
    modes = options.get("device_modes", {})
    result["device_modes"] = {key: modes.get(key, "control_verification")
        if previous.get(key) == members else "control_verification"
        for key, members in groups.items()}
    result["planning_admissions"] = groups
    return result


def execution_mode_options(options, devices, device_key, mode):
    """Persist the same local permission exposed on every Planned device card."""
    if mode not in MODES:
        raise ValueError("Choose Verification or Controlling")
    device = next((d for d in devices if d['key'] == device_key), None)
    if device is None or not device['planned']:
        raise ValueError("Only Planned, Included devices have execution permission")
    if mode == 'controlling' and device['permission']['reason']:
        raise ValueError(device['permission']['reason'])
    owner = '$' + device['system'] if device.get('system') else device_key
    result = deepcopy(options)
    result['device_modes'] = {**result.get('device_modes', {}), owner: mode}
    return result
