"""Shared device view and execution-mode action for the panel and HA entities."""
from datetime import datetime, timezone

from . import const as shs_const
from .configuration import (resolved_options, entity_display_name_by_id,
    area_name_by_id, entity_area_id_by_id, suggest_device_control_mapping)
from .configuration_fields import _control_fields
from .configuration_schema import initialise_device_inclusion
from .device_controls import apply_planner_support, mapping_report, is_room_thermal_control, mapped_planning_path
from .operating_modes import execution_mode_options
from .presentation import complete_device_views


def execution_device_views(hass, entry, choices, *, include_suggestions=True):
    """Use exactly the Schedule tab's inventory, ownership and permission rules."""
    coordinator = entry.runtime_data
    options = resolved_options(hass, dict(entry.options))
    requested = choices['devices']
    mappings = options.get(shs_const.OPT_DEVICE_CONTROL_MAPPINGS, {})
    if not isinstance(mappings, dict):
        mappings = {}
    known_entity_ids = {state.entity_id for state in hass.states.async_all()}
    entity_names = entity_display_name_by_id(hass)
    area_names = area_name_by_id(hass)
    entity_area_ids = entity_area_id_by_id(hass)
    devices = []
    for device in requested:
        control_type = str(device.get("control_type") or (mappings.get(device["key"]) or {}).get("control_type") or "")
        saved = mappings.get(device["key"])
        saved_mapping = (
            dict(saved)
            if isinstance(saved, dict) and saved.get("control_type") == control_type
            else {}
        )
        report = apply_planner_support(
            mapping_report(
                control_type,
                saved,
                known_entity_ids,
                entity_names,
                area_names,
                entity_area_ids,
                pool_water_entity=options.get("pool_water_temperature_entity"),
                pool_control=mapped_planning_path(device, saved_mapping, options.get("pool_water_temperature_entity")) == "pool",
                room_control=is_room_thermal_control(
                    control_type, str(device.get("category") or "")
                ),
            ),
            control_type,
            str(device.get("category") or ""),
        )
        devices.append(
            {
                "key": device["key"],
                "name": str(device.get("name") or device["key"]),
                "statistic_id": device.get("statistic_id") or device["key"],
                "category": device.get("category"),
                "load_type": device.get("load_type"),
                "planning_role": device.get("planning_role"),
                "planning_choice_at": device.get("planning_choice_at"),
                "control_type": control_type,
                "mapping": saved_mapping,
                "stale_mapping_control_type": (
                    saved.get("control_type")
                    if isinstance(saved, dict)
                    and saved.get("control_type") != control_type
                    else None
                ),
                "suggested_mapping": (suggest_device_control_mapping(
                    hass, device, control_type
                ) if include_suggestions else {}),
                "execution_status": coordinator.controller.status.get("device:" + device["key"]),
                "fields": list(_control_fields({**device, "control_type": control_type})),
                **report,
            }
        )


    return complete_device_views(devices, options, choices, coordinator.operational_status,
        coordinator.optimisation_plan or {}, coordinator.controller.status, entity_names,
        area_names, datetime.now(timezone.utc), entry.options.keys())


async def async_execution_devices(hass, entry, choices=None, *, include_suggestions=True):
    if choices is None:
        choices = await entry.runtime_data.async_cached_planning_configuration()
    devices = execution_device_views(hass, entry, choices, include_suggestions=include_suggestions)
    initialised = initialise_device_inclusion(dict(entry.options), devices)
    if initialised != dict(entry.options):
        hass.config_entries.async_update_entry(entry, options=initialised)
        entry.runtime_data._plan_configuration_changed = True
        devices = execution_device_views(hass, entry, choices, include_suggestions=include_suggestions)
    return devices


async def async_set_execution_mode(hass, entry, device_key, mode):
    """One validation/persistence path, regardless of which UI invokes it."""
    devices = await async_execution_devices(hass, entry)
    previous = dict(entry.options)
    options = execution_mode_options(previous, devices, device_key, mode)
    if options == previous:
        return
    options[shs_const.OPT_CONFIGURATION_REVIEWED_AT] = datetime.now(timezone.utc).isoformat()
    hass.config_entries.async_update_entry(entry, options=options)
    coordinator = entry.runtime_data
    coordinator.async_update_listeners()
    # Verification and Controlling change who may write, never the plan: the
    # retained schedule is executed or verified as it is, with no exchange.
    await coordinator.async_battery_inputs_refresh()
    await coordinator.controller.async_tick()
