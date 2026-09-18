"""Customer-facing views, independent of persistence and device writes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re
if __package__:
    from .optimisation import validate_plan_contract, OptimisationInputError
    from .configuration_fields import _configuration_sections, _control_fields, LABELS
    from .device_controls import planning_path, mapped_planning_path, battery_control_errors, pool_control_errors
    from .device_commands import execution_setup_errors
else:
    from optimisation import validate_plan_contract, OptimisationInputError
    from configuration_fields import _configuration_sections, _control_fields, LABELS
    from device_controls import planning_path, mapped_planning_path, battery_control_errors, pool_control_errors
    from device_commands import execution_setup_errors



def device_name(name):
    """Only a display transformation. The meter key is never changed."""
    value = str(name).strip()
    stripped = re.sub(r"(?:[\s·_-]+(?:energy|power|consumption|kwh|meter|sensor))+$", "", value, flags=re.I).strip()
    return stripped or value


def operational_status(plan, mode, missing, now, *, options=None):
    result = {"state": "unavailable", "reason": "Waiting for a plan", "actionable": False,
              "now": now.isoformat(), "plan_id": (plan or {}).get("plan_id"),
              **{key: (plan or {}).get(key) for key in ("issued_at", "binding_until", "valid_until")}}
    if mode == "disabled":
        result.update(state="disabled", reason="Monitoring continues while planning is off")
    elif missing and not plan:
        result.update(state="not_configured", reason="Complete the required planning inputs")
    elif plan:
        try:
            issued = datetime.fromisoformat(plan["issued_at"])
            # Validate structure even for an expired plan, without treating it as executable.
            valid_until = datetime.fromisoformat(plan["valid_until"])
            check_at = issued if now >= valid_until else now
            validate_plan_contract(plan, check_at, require_recent_issue=False)
            selected = plan
            scope_changed = False
            if options is not None:
                if __package__:
                    from .operating_modes import operating_mode_identity
                else:
                    from operating_modes import operating_mode_identity
                modes = operating_mode_identity(options, plan.get("operating_scope", {}).get("device_owners", {}).values())
                scope_changed = plan.get("operating_scope", {}).get("modes") != modes
                if not scope_changed and "controlling" in modes.values():
                    selected = plan["execution_plan"]
            if scope_changed:
                result.update(state="not_configured", reason="Waiting for a plan for the current device modes")
            elif now >= valid_until:
                result.update(state="expired", reason="The last plan has expired")
            elif selected["status"] != "ready":
                result.update(state=selected["status"], reason=LABELS[selected["status"]])
            elif now >= datetime.fromisoformat(plan["binding_until"]):
                result.update(state="ready", reason="Executing cached schedule using estimated prices", actionable=True)
            else:
                result.update(state="ready", reason="A validated plan is available", actionable=True)
        except (OptimisationInputError, KeyError, TypeError, ValueError) as err:
            result.update(state="invalid", reason=str(err))
    result["label"] = LABELS[result["state"]]
    return result


def timeline(plan, status, *, command_preview=None, options=None):
    """Never expose an invalid/expired schedule as actionable instructions."""
    if status["state"] not in {"ready", "advisory_only"}:
        return {"capabilities": {}, "slots": [], "reason": status["reason"]}
    if plan.get("schema_version") == 9 and options is not None:
        if __package__:
            from .operating_modes import operating_mode_identity
        else:
            from operating_modes import operating_mode_identity
        if plan["operating_scope"]["modes"] != operating_mode_identity(options, plan["operating_scope"]["device_owners"].values()):
            return {"capabilities": {}, "slots": [], "reason": "Waiting for a plan for the current device modes"}
    execution = timeline(plan["execution_plan"], status, command_preview=command_preview) if plan.get("execution_plan") else None
    return {"capabilities": deepcopy(plan.get("capabilities", {})), "slots": [
        {"start": slot["start"], "binding": slot["binding"],
         **({"execution": {**execution["slots"][i], "capabilities": execution["capabilities"]}} if execution else {}),
         "commands": deepcopy(slot.get("device_commands", {})),
         "battery_command": deepcopy(slot.get("battery_command")),
         "command_previews": command_preview(slot) if command_preview is not None else {},
         # Shadow prices are what the planner valued each quarter at: the
         # published price where one exists, otherwise the server's estimate.
         **{key: slot.get(key) for key in ("battery_charge_w", "battery_discharge_w", "ev_target_current_a", "pool_w",
                                           "shadow_import_sek_per_kwh", "shadow_export_sek_per_kwh")}}
        for i, slot in enumerate(plan["plans"]["priority"]["slots"])
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


def execution_view(mode, status):
    """Mode eligibility and the last controller outcome are different facts."""
    if mode in {"monitoring", "planning"} and (not status or status.get("state") not in {"fault", "overridden"}):
        return {"state": mode, "reason": "Observing only; no SHS commands" if mode == "monitoring" else
                "Planning only; no SHS commands or command verification"}
    return deepcopy(status) if status else {"state": "idle", "reason": "No controller evaluation recorded"}


def device_readiness(devices):
    """Count included equipment, using the same rows the customer sees."""
    included = [device for device in devices if device.get("included")]
    return {
        "definition": "ready_devices counts mapping completeness only; it does not establish executable planning or control readiness.",
        "planning_supported_devices": sum(d.get("planning_support", {}).get("state") == "available" for d in included),
        "execution_eligible_devices": sum(d.get("execution_eligibility", {}).get("eligible", False) for d in devices),
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
    if __package__:
        from .operating_modes import device_mode, system_device_keys
    else:
        from operating_modes import device_mode, system_device_keys
    devices = deepcopy(devices)
    for device in devices:
        key = device.get("statistic_id") or device["key"]
        device["name"] = device_name(entity_names.get(key) or device.get("name") or key)
        device["room_name"] = area_names.get(device.get("mapping", {}).get("room_area_id")) or device.get("mapping_summary", {}).get("room_name") or "No room"
    owners = system_device_keys(devices, options)
    for system in ("ev", "pool"):
        category = {"ev": "ev_charging", "pool": "pool_heating"}[system]
        if not equipment_present(options, system, devices, configured_keys):
            devices = [d for d in devices if d.get("category") != category or planning_path(d.get("control_type"), category) == "room"]
            continue
        candidates = [d for d in devices if owners.get(d["key"]) == system]
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
    active = next((slot for slot in (plan or {}).get("plans", {}).get("priority", {}).get("slots", [])
                   if datetime.fromisoformat(slot["start"]) <= now < datetime.fromisoformat(slot["start"]) + timedelta(minutes=15)), None) if status["actionable"] else None
    for device in devices:
        key = device["key"]
        mapping = device.get("mapping", {})
        system = device.get("system")
        included = key not in options.get("excluded_device_readings", [])
        planned = included and device.get("planning_role") == "controllable"
        device["included"] = included
        device["planned"] = planned
        controller_id = system or "device:" + key
        reason = None
        if not planned:
            reason = "Choose Planned on the website before enabling execution"
        elif key in options.get("excluded_device_readings", []):
            reason = "Include this device before enabling execution"
        elif not fresh:
            reason = "Refresh the website choices before enabling control"
        elif system == "battery":
            reason = "; ".join(battery_control_errors({**options, "battery_control_enabled": True})) or None
        elif system == "pool":
            reason = "; ".join(pool_control_errors(options, mapping)) or device.get("mapping_error") or (
                "Complete the pool heater's setup first" if device.get("mapping_status") != "ready" else None)
        elif system == "ev":
            if not options.get("ev_charge_switch_entity") or device.get("mapping_status") != "ready":
                reason = "Set up the charging current and start/stop switch first"
        else:
            errors = execution_setup_errors(mapping)
            if device.get("control_type") not in {"setpoint", "switch_schedule", "permit_inhibit"}:
                errors = [LABELS.get(device.get("control_type"), "The method") + " selected on the website is not supported for this device"]
            if errors:
                reason = "; ".join(errors)
            elif device.get("mapping_status") != "ready":
                reason = "Complete this device's setup first"
        verification_reason = reason
        if reason is None:
            if not status["actionable"]:
                reason = status["reason"]
            elif system:
                if not (plan or {}).get("capabilities", {}).get(system):
                    reason = "Waiting for a plan for this device"
            else:
                commands = [slot.get("device_commands", {}).get(key) for slot in (plan or {}).get("plans", {}).get("priority", {}).get("slots", []) if slot.get("binding") and datetime.fromisoformat(slot["start"]) <= now < datetime.fromisoformat(slot["start"]) + timedelta(minutes=15)]
                command = next((c for c in commands if c), None)
                if not command or command.get("type") == "unavailable":
                    reason = command.get("reason") if command else "Waiting for instructions for this device"
        mode = device_mode(options, controller_id)
        device["mode"] = mode
        device["permission"] = {"enabled": mode == "controlling", "reason": reason, "verification_reason": verification_reason, "controller_id": controller_id}
        device["last_controller_result"] = deepcopy(controllers.get(controller_id))
        device["execution_status"] = execution_view(mode, controllers.get(controller_id))
        device["optimisation_included"] = planned
        device["mapping_readiness"] = {"state": device.get("mapping_status"), "reason": device.get("mapping_error")}
        command = (active or {}).get("device_commands", {}).get(key)
        if not status["actionable"]:
            planning_reason = status["reason"]
        elif system:
            planning_reason = None if active and (plan or {}).get("capabilities", {}).get(system) else "Waiting for a plan for this device"
        else:
            planning_reason = command.get("reason") if command and command.get("type") == "unavailable" else None if command else "Waiting for instructions for this device"
        device["planning_support"] = {"state": "available" if planning_reason is None else "unavailable",
                                      "reason": planning_reason, "path": system or "device_commands"}
        device["execution_reason"] = planning_reason
        device["execution_eligibility"] = {"eligible": mode in {"controlling", "control_verification"} and reason is None,
            "writes_permitted_by_mode": mode == "controlling", "verification_permitted_by_mode": mode == "control_verification",
            "reason": "Inactive by operating mode" if mode in {"monitoring", "planning"} else reason}
        device["system_fields"] = system_fields(system) if system else []
        device["planning_fields"] = [field for field in device["system_fields"] if field["key"] in PLANNING_FIELDS]
        device["planning_system"] = mapped_planning_path(
            device, mapping, options.get("pool_water_temperature_entity")
        )
        if system == "pool":
            device["fields"] = list(_control_fields(device))
            errors = {}
            gaps = pool_control_errors(options, mapping, field_errors=errors)
            device["field_errors"] = {**device.get("field_errors", {}), **errors}
            if gaps:
                device["mapping_status"] = "invalid"
                device["mapping_error"] = "; ".join(gaps)
            device["mapping_readiness"] = {"state": device["mapping_status"], "reason": device["mapping_error"]}
            for field in device["system_fields"]:
                if field["key"] == "pool_water_temperature_entity":
                    field["required"] = included

        device["controller_explanation"] = controller_explanation(
            controller_id, mode, device["execution_status"], active)["explanation"]
        device["fields"] = [f for f in device.get("fields", []) if f["key"] != "control_enabled"]
        if not planned:
            device["mapping_error"] = None
            device["execution_reason"] = None
    return devices


def battery_status_text(runtime):
    """Identical live wording for the card, status sensor and mode select."""
    explanation = runtime.get('explanation') or {}
    current = explanation.get('now') or (
        'Complete the highlighted measurement settings.' if (runtime.get('fix') or {}).get('kind') == 'fields'
        else 'Waiting for current household and battery readings.')
    if (runtime.get('measurements') or {}).get('response_matches_direction') is False and runtime.get('mode') == 'controlling':
        current += ' The battery has not yet responded as requested.'
    if runtime.get('pending_writes'):
        current += ' Waiting for the battery to confirm its settings.'
    measured = (runtime.get('loss_evidence') or {}).get('discharge', {}).get('model_source') == 'measured'
    loss = ('Energy-loss estimates use measurements from your system.' if measured else
            'Energy-loss estimates use your settings while measurements are collected.' if runtime.get('loss_model') else '')
    warning = ''
    if runtime.get('plan_rejection'):
        reference = ('The previously accepted plan remains in use while it is valid.' if runtime.get('accepted_reference_id')
                     else 'There is no accepted battery plan to use.')
        warning = 'A new battery plan could not be accepted. ' + reference + ' Waiting for a corrected plan.'
    return {'status': runtime.get('reason') or explanation.get('status') or 'Waiting for the battery controller.',
            'now': current, 'loss': loss, 'plan_warning': warning}


def controller_explanation(device, mode, status, slot=None):
    """Plain-language details; requests never assert physical delivery."""
    status = status or {}
    runtime = status.get('battery_runtime')
    if device == 'battery' and runtime is not None:
        live = battery_status_text(runtime)
        explanation = runtime.get('explanation') or {}
        parts = [live['status'], live['plan_warning'], live['now'], live['loss']]
        outlook = [explanation.get(key) for key in ('plan', 'difference', 'next')]
        text = '\n'.join(p for p in parts if p)
        if any(outlook): text += '\n\n' + '\n'.join(p for p in outlook if p)
        result = {'explanation': text, **{key: runtime.get(key) for key in
            ('plan_status', 'plan_rejection', 'accepted_plan_id', 'accepted_reference_id')}}
        if explanation.get('deadline_ms') is not None:
            result['plan_target_deadline'] = datetime.fromtimestamp(explanation['deadline_ms']/1000, timezone.utc).isoformat()
        return result
    intro = ('Testing the plan; device settings are not being changed.' if mode == 'control_verification' else
             'SHS is allowed to adjust this device to follow the plan.' if mode == 'controlling' else
             'Observing this device; SHS is not changing its settings.')
    parts = [intro]
    if status.get("control_notice"): parts.append(status["control_notice"])
    reason = status.get('reason')
    if reason == 'Commands logged; physical response and cross-slot transitions are not tested':
        reason = 'Proposed settings have been recorded. The equipment has not been tested with these settings.'
    if reason: parts.append(reason)
    if status.get('water_temperature_c') is not None:
        parts.append(f"The pool water is {status['water_temperature_c']:.1f} °C.")
    if status.get('stop_temperature_c') is not None:
        parts.append(f"Your Stop at temperature is {status['stop_temperature_c']:.1f} °C.")
    if status.get('decision_reason'):
        parts.append(status['decision_reason'].replace('is allowed to run', 'would be allowed to run').replace('is off', 'would be off')
                     if mode == 'control_verification' else status['decision_reason'])
    if status.get('measured_power_w') is not None:
        parts.append(f"This device is using {status['measured_power_w']/1000:.2f} kW.")
    planned = None
    if slot:
        if device == 'ev' and 'ev_target_current_a' in slot:
            current = slot['ev_target_current_a']
            planned = f'The plan requests charging at {current:g} A.' if current else 'The plan requests no car charging now.'
        elif device == 'pool' and 'pool_w' in slot:
            planned = 'The plan requests pool heating now.' if slot['pool_w'] > 0 else 'The plan requests no pool heating now.'
        else:
            command = slot.get('device_commands', {}).get(device.removeprefix('device:'), {})
            kind = command.get('type')
            if kind == 'setpoint': planned = f"The plan requests a temperature of {command['target_c']:g} °C."
            elif kind == 'switch_schedule': planned = 'The plan requests this device to be on.' if command['on_seconds'] else 'The plan requests this device to be off.'
            elif kind == 'permit_inhibit': planned = 'The plan allows this device to run.' if command['permitted'] else 'The plan requests a pause.'
            elif kind == 'variable_power': planned = f"The plan requests {command['value']:g} {command['unit']}."
            elif kind == 'unavailable': planned = command.get('reason')
    parts.append(planned or 'Waiting for a current plan for this device.')
    if status.get('next_step'): parts.append(status['next_step'])
    return {'explanation': '\n'.join(parts)}
