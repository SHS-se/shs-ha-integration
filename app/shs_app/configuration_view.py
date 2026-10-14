"""The app's editor payload, preserving shared readiness and correction fields."""
from shs_core import const as shs_const
from shs_core.api import ShsApiError
from shs_core.configuration_fields import LABELS, _configuration_sections
from shs_core.presentation import timeline, system_fields, device_name, device_readiness
from shs_core.device_controls import room_thermal_zones

async def configuration_payload(editor, *, refresh_roles):
    coordinator = editor.engine.household
    portal_error: str | None = None
    try:
        requested = (
            await coordinator.async_refresh_device_configuration()
            if refresh_roles
            else await coordinator.async_cached_device_configuration()
        )
    except (ShsApiError, KeyError, TypeError, ValueError) as err:
        portal_error = str(err)
        requested = await coordinator.async_cached_device_configuration()

    choices = await coordinator.async_cached_planning_configuration()
    requested = choices["devices"]
    options = editor.resolved_options()
    exchange_status = await coordinator.async_cached_exchange_status()
    known_entity_ids = set(editor.catalog.states)
    entity_names = editor.context['entity_names']
    devices = await editor.devices(choices)
    options = editor.resolved_options()

    thermal_devices = room_thermal_zones(devices)
    ready_thermal_devices = [
        device
        for device in thermal_devices
        if device["mapping_status"] == "ready"
        and isinstance(device.get("mapping_summary", {}).get("room_key"), str)
    ]
    mapped_room_keys = {
        device["mapping_summary"].get("room_key")
        for device in ready_thermal_devices
        if isinstance(device.get("mapping_summary"), dict)
        and isinstance(device["mapping_summary"].get("room_key"), str)
    }
    outdoor_entity = options.get(shs_const.OPT_OUTDOOR_TEMPERATURE_ENTITY)
    weather_entity = options.get(shs_const.OPT_WEATHER_FORECAST_ENTITY)
    outdoor_ready = bool(outdoor_entity and outdoor_entity in known_entity_ids)
    forecast_ready = bool(weather_entity and weather_entity in known_entity_ids)
    thermal_slots = int(coordinator.last_thermal_slots_accepted or 0)
    thermal_accepted_until = exchange_status.get("thermal_slots_accepted_until")
    if not thermal_devices:
        thermal_status = "not_requested"
    elif len(ready_thermal_devices) != len(thermal_devices):
        thermal_status = "device_mappings_required"
    elif not outdoor_ready or not forecast_ready:
        thermal_status = "outdoor_sources_required"
    elif thermal_slots == 0 and not thermal_accepted_until:
        thermal_status = "waiting_for_history"
    else:
        thermal_status = "observations_published"

    plan = coordinator.optimisation_plan or {}
    operation = coordinator.operational_status
    battery_required = any(device.get("system") == "battery" and device["included"] for device in devices)
    coordinator._sync_battery_control_issue({**options, "battery_control_enabled": True}, included=battery_required)
    for device in devices:
        if device.get("system") == "battery":
            device["live_inputs"] = coordinator.battery_live_inputs.snapshot()
            device["battery_writer"] = coordinator.battery_writer.snapshot()
            device["battery_runtime"] = coordinator.battery_runtime.snapshot()
            device["execution_status"] = {key: device["battery_runtime"].get(key)
                for key in ("state", "reason", "fix", "next_step", "retry_automatically", "plan_status", "plan_rejection", "technical_error", "decision")}
        mapping = device.get("mapping", {})
        source_ids = [mapping.get("temperature_entity_id"), mapping.get("power")]
        if device.get("system"):
            system = device["system"]
            source_ids.extend([options.get(system + "_soc_entity"), options.get(system + "_water_temperature_entity")])
        device["readings"] = []
        for entity_id in dict.fromkeys(value for value in source_ids if isinstance(value, str)):
            state = editor.catalog.states.get(entity_id)
            if state:
                device["readings"].append({"name": state.attributes.get("friendly_name") or "Reading",
                    "value": state.state, "unit": state.attributes.get("unit_of_measurement", ""),
                    "updated_at": state.last_updated.isoformat()})
    return {
        "labels": LABELS,
        "operation": operation,
        "replan_recommendations": getattr(coordinator, "replan_recommendations", []),
        # The same list the website shows: devices this plan leaves out.
        "measurement_issues": [issue for issue in (plan or {}).get("measurement_issues") or [] if isinstance(issue, dict)],
        "timeline": timeline(plan, operation, command_preview=coordinator.controller.preview_commands, options=options),
        "website_url": shs_const.website_url(editor.engine.household.client.base_url, "/portal/energy-modeling?tab=devices"),
        "configured_keys": list(editor.engine.configuration.options()),
        "meter_inventory": [{"key": d["key"], "name": device_name(entity_names.get(d["key"]) or d["name"])} for d in requested],
        "entry": {
            "entry_id": editor.engine.identity['entry_id'],
            "title": 'Smart Home Solutions',
            "state": 'loaded',
        },
        "configuration": options,
        "sections": _configuration_sections(battery_control_required=battery_required),
        "entities": editor.entities(),
        "devices": devices,
        "portal": {
            "status": "error" if portal_error else "synchronised",
            "refreshed_at": choices.get("refreshed_at"),
            "error": portal_error,
            "requested_devices": device_readiness(devices)["requested_devices"],
        },
        # Everything currently asking for a decision, with a resolved link
        # where the fix lives on the website. Same source as the Home Assistant
        # repairs, so the panel cannot show green while a warning is up.
        "attention": [
            {
                **item,
                "fix": (
                    {
                        **item["fix"],
                        "url": shs_const.website_url(
                            editor.engine.household.client.base_url,
                            item["fix"].get("path", "/portal"),
                        ),
                    }
                    if item["fix"].get("kind") == "website"
                    else item["fix"]
                ),
            }
            for item in coordinator.attention_items
        ],
        "readiness": {
            "planning_mode": options.get(shs_const.OPT_PLANNING_MODE),
            **device_readiness(devices),
            "missing_inputs": list(coordinator.optimisation_missing_inputs),
            "last_plan_error": coordinator.last_optimisation_error,
            "planning_job": exchange_status.get("planning_job"),
            "planning_submission": exchange_status.get("planning_submission"),
            "last_plan_attempt": coordinator.last_optimisation_attempt or exchange_status.get("last_optimisation_attempt"),
            "last_plan_push": (
                coordinator.last_optimisation_push
                or exchange_status.get("last_optimisation_push")
            ),
            "plan_status": operation["state"],
            "plan_model_version": plan.get("model_version"),
            "actual_slots_accepted": coordinator.last_actual_slots_accepted,
            "actuals_accepted_until": coordinator.actuals_accepted_until or exchange_status.get("actuals_accepted_until"),
        },
        "thermal": {
            "status": thermal_status,
            "requested_zones": len(thermal_devices),
            "mapped_zones": len(ready_thermal_devices),
            "mapped_rooms": len(mapped_room_keys),
            "outdoor_temperature_entity": outdoor_entity,
            "outdoor_temperature_ready": outdoor_ready,
            "weather_forecast_entity": weather_entity,
            "weather_forecast_ready": forecast_ready,
            "last_slots_accepted": thermal_slots,
            "accepted_until": thermal_accepted_until,
            "zones": [
                {
                    "key": device["key"],
                    "name": device["name"],
                    "room_name": device["mapping_summary"].get("room_name")
                    if isinstance(device.get("mapping_summary"), dict)
                    else None,
                    "mapping_status": device["mapping_status"],
                    "mapping_error": device["mapping_error"],
                }
                for device in thermal_devices
            ],
        },
        "diagnostics": {
            "controllers": dict(coordinator.controller.status),
            "migration": options.get("_migration_report"),
            "subscription_active": bool((coordinator.data or {}).get("subscription_active")),
            "tariff_status": coordinator.tariff_status,
            "last_tariff_error": coordinator.last_tariff_error,
            "last_daily_push": coordinator.last_push_date,
            "last_daily_push_error": coordinator.last_push_error,
            "last_optimisation_error": coordinator.last_optimisation_error,
            "last_runtime_report": coordinator.last_runtime_report,
            "last_runtime_error": coordinator.last_runtime_error,
            "last_thermal_slots_accepted": thermal_slots,
            "thermal_slots_accepted_until": thermal_accepted_until,
        },
    }

