"""Customer-facing views, independent of persistence and device writes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re
if __package__:
    from .optimisation import validate_plan_contract, OptimisationInputError
    from .configuration_fields import _configuration_sections, _control_fields, LABELS
    from .device_controls import planning_path, mapped_planning_path, battery_control_errors, pool_band_errors
    from .device_commands import execution_setup_errors
else:
    from optimisation import validate_plan_contract, OptimisationInputError
    from configuration_fields import _configuration_sections, _control_fields, LABELS
    from device_controls import planning_path, mapped_planning_path, battery_control_errors, pool_band_errors
    from device_commands import execution_setup_errors



def device_name(name):
    """Only a display transformation. The meter key is never changed."""
    value = str(name).strip()
    stripped = re.sub(r"(?:[\s·_-]+(?:energy|power|consumption|kwh|meter|sensor))+$", "", value, flags=re.I).strip()
    return stripped or value


def operational_status(plan, mode, missing, now):
    result = {"state": "unavailable", "reason": "Waiting for a plan", "actionable": False,
              "now": now.isoformat(), "plan_id": (plan or {}).get("plan_id"),
              **{key: (plan or {}).get(key) for key in ("issued_at", "binding_until", "valid_until")}}
    if mode == "disabled":
        result.update(state="disabled", reason="Monitoring continues while planning is off")
    elif missing:
        result.update(state="not_configured", reason="Complete the required planning inputs")
    elif plan:
        try:
            issued = datetime.fromisoformat(plan["issued_at"])
            # Validate structure even for an expired plan, without treating it as executable.
            valid_until = datetime.fromisoformat(plan["valid_until"])
            check_at = issued if now >= valid_until else now
            validate_plan_contract(plan, check_at, require_recent_issue=False)
            if now >= valid_until:
                result.update(state="expired", reason="The last plan has expired")
            elif plan["status"] != "ready":
                result.update(state=plan["status"], reason=LABELS[plan["status"]])
            elif now >= datetime.fromisoformat(plan["binding_until"]):
                result.update(state="advisory_only", reason="Future estimates are advice only")
            else:
                result.update(state="ready", reason="A validated plan is available", actionable=True)
        except (OptimisationInputError, KeyError, TypeError, ValueError) as err:
            result.update(state="invalid", reason=str(err))
    result["label"] = LABELS[result["state"]]
    return result


def timeline(plan, status):
    """Never expose an invalid/expired schedule as actionable instructions."""
    if status["state"] not in {"ready", "advisory_only"}:
        return {"capabilities": {}, "slots": [], "reason": status["reason"]}
    return {"capabilities": deepcopy(plan.get("capabilities", {})), "slots": [
        {"start": slot["start"], "binding": slot["binding"],
         "commands": deepcopy(slot.get("device_commands", {})),
         **{key: slot.get(key) for key in ("battery_charge_w", "battery_discharge_w", "ev_target_current_a", "pool_w")}}
        for slot in plan["plans"]["priority"]["slots"]
    ], "reason": None}


PLANNING_FIELDS = {
    "pool_volume_m3", "ev_charge_efficiency", "ev_kwh_per_km",
    "battery_capacity_kwh", "battery_target_soc", "battery_target_is_hard",
    "battery_charge_efficiency", "battery_discharge_efficiency",
    "battery_export_enabled", "battery_export_reserve_soc",
    "battery_export_min_price_sek_per_kwh", "terminal_soc_min",
    "terminal_energy_value_sek_per_kwh",
}


def system_fields(system):
    """One owner for every system field, reused by the editor and write guard."""
    result = []
    for section in _configuration_sections():
        for field in section["fields"]:
            key = field["key"]
            belongs = key.startswith(system + "_") or (system == "battery" and key.startswith("terminal_"))
            if belongs and key not in {system + "_control_enabled", system + "_enabled"}:
                result.append(deepcopy(field))
    return result


def device_readiness(devices):
    """Count included equipment, using the same rows the customer sees."""
    included = [device for device in devices if device.get("included")]
    return {
        "requested_devices": len(included),
        "ready_devices": sum(device.get("mapping_status") == "ready" for device in included),
        "device_mapping_gaps": [device["name"] for device in included if device.get("mapping_status") != "ready"],
    }


def equipment_present(options, system, devices, configured_keys=()):
    if not options.get(system + "_enabled"):
        return False
    evidence = {"battery": "battery_soc_entity", "ev": "ev_connected_entity", "pool": "pool_water_temperature_entity"}
    category = {"ev": "ev_charging", "pool": "pool_heating"}.get(system)
    return bool(system + "_enabled" in configured_keys or options.get(evidence[system]) or (category and any(d.get("category") == category and planning_path(d.get("control_type"), category) != "room" for d in devices)))


def complete_device_views(devices, options, choices, status, plan, controllers, entity_names, area_names, now, configured_keys=()):
    """Augment one inventory with website choice, setup owner and local permission."""
    devices = deepcopy(devices)
    for device in devices:
        key = device.get("statistic_id") or device["key"]
        device["name"] = device_name(entity_names.get(key) or device.get("name") or key)
        device["room_name"] = area_names.get(device.get("mapping", {}).get("room_area_id")) or device.get("mapping_summary", {}).get("room_name") or "No room"
    for system in ("ev", "pool"):
        category = {"ev": "ev_charging", "pool": "pool_heating"}[system]
        if not equipment_present(options, system, devices, configured_keys):
            devices = [d for d in devices if d.get("category") != category or planning_path(d.get("control_type"), category) == "room"]
            continue
        candidates = [d for d in devices if d.get("category") == category and mapped_planning_path(d, d.get("mapping", {}), options.get("pool_water_temperature_entity")) != "room"]
        candidates.sort(key=lambda d: (d.get("control_type") != "setpoint", d["key"]))
        if candidates:
            candidates[0]["system"] = system
        else:
            devices.append({"key": "$" + system, "name": LABELS[system], "category": category,
                            "mapping": {}, "fields": [], "system": system, "mapping_status": "not_configured"})
    if equipment_present(options, "battery", devices, configured_keys):
        battery = choices.get("home", {}).get("battery", {})
        devices.append({"key": "$battery", "name": "House battery", "category": "battery", "system": "battery",
                        "planning_role": "controllable" if battery.get("included") else "base_load",
                        "planning_choice_at": battery.get("choice_at"), "mapping": {}, "fields": [], "mapping_status": "not_configured" if battery_control_errors({**options, "battery_control_enabled": True}) else "ready"})
    refreshed = choices.get("refreshed_at")
    try:
        fresh = now - datetime.fromisoformat(refreshed) < timedelta(minutes=30)
    except (TypeError, ValueError):
        fresh = False
    for device in devices:
        key = device["key"]
        mapping = device.get("mapping", {})
        system = device.get("system")
        included = device.get("planning_role") == "controllable"
        device["included"] = included
        device["choice_label"] = ("Included" if included else "Excluded") + (" · not reviewed" if not device.get("planning_choice_at") else "")
        if not refreshed or (system == "battery" and "battery" not in choices.get("home", {})):
            device["choice_label"] = "Waiting for website choices"
        enabled = bool(options.get(system + "_control_enabled")) if system else bool(mapping.get("control_enabled"))
        controller_id = system or "device:" + key
        reason = None
        if not included:
            reason = "Include this device in the plan on the website first"
        elif key in options.get("excluded_device_readings", []):
            reason = "Share individual readings for this device before enabling control"
        elif not fresh:
            reason = "Refresh the website choices before enabling control"
        elif not status["actionable"]:
            reason = status["reason"]
        elif system:
            if not (plan or {}).get("capabilities", {}).get(system):
                reason = "Waiting for a plan for this device"
            elif system == "battery":
                reason = "; ".join(battery_control_errors({**options, "battery_control_enabled": True})) or None
            elif system == "pool":
                reason = "; ".join(pool_band_errors(options)) or None
                if not options.get("pool_start_temperature_entity"):
                    reason = "Set up the pool temperature controls first"
            elif not options.get("ev_charge_switch_entity") or device.get("mapping_status") != "ready":
                reason = "Set up the charging current and start/stop switch first"
        else:
            errors = execution_setup_errors(mapping)
            if device.get("control_type") not in {"setpoint", "switch_schedule", "permit_inhibit"}:
                errors = [LABELS.get(device.get("control_type"), "The method") + " selected on the website is not supported for this device"]
            if errors:
                reason = "; ".join(errors)
            elif device.get("mapping_status") != "ready":
                reason = "Complete this device's setup first"
            else:
                commands = [slot.get("device_commands", {}).get(key) for slot in (plan or {}).get("plans", {}).get("priority", {}).get("slots", []) if slot.get("binding") and datetime.fromisoformat(slot["start"]) <= now < datetime.fromisoformat(slot["start"]) + timedelta(minutes=15)]
                command = next((c for c in commands if c), None)
                if not command or command.get("type") == "unavailable":
                    reason = command.get("reason") if command else "Waiting for instructions for this device"
        device["permission"] = {"enabled": enabled, "reason": reason, "controller_id": controller_id}
        device["execution_status"] = controllers.get(controller_id, {"state": "disabled"})
        device["system_fields"] = system_fields(system) if system else []
        device["planning_fields"] = [field for field in device["system_fields"] if field["key"] in PLANNING_FIELDS]
        device["planning_system"] = mapped_planning_path(
            device, mapping, options.get("pool_water_temperature_entity")
        )
        if device["planning_system"] == "pool" and device.get("control_type") == "setpoint":
            # The shared pool sensor has one editor, on the pool Controls card.
            device["fields"] = [f for f in device.get("fields", []) if f["key"] != "temperature_entity_id"]

        device["fields"] = [f for f in device.get("fields", []) if f["key"] != "control_enabled"]
        if not included:
            device["mapping_error"] = None
            device["execution_reason"] = None
    return devices
