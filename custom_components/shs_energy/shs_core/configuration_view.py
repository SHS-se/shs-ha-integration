"""Shared configuration read model owned by the app."""
from datetime import datetime, timezone
from . import const as shs_const
from .configuration_schema import resolve_configuration
from .configuration_fields import _control_fields
from .discovery import suggest_device_control_mapping
from .device_controls import apply_planner_support, mapping_report, is_room_thermal_control, mapped_planning_path, pool_mapping
from .presentation import complete_device_views

def execution_device_views(catalog, context, explicit, choices, coordinator, *, include_suggestions=True):
    """Use exactly the Schedule tab's inventory, ownership and permission rules."""
    options = resolve_configuration(explicit, catalog.latitude, catalog.longitude)
    requested = choices['devices']
    mappings = options.get(shs_const.OPT_DEVICE_CONTROL_MAPPINGS, {})
    if not isinstance(mappings, dict):
        mappings = {}
    known_entity_ids = set(catalog.states)
    entity_names = context['entity_names']
    area_names = context['area_names']
    entity_area_ids = context['entity_areas']
    devices = []
    for device in requested:
        setup = device.get("app_setup")
        control_type = (setup["control_type"] if setup else
                        device.get("control_type") or (mappings.get(device["key"]) or {}).get("control_type")) or ""
        saved = mappings.get(device["key"])
        path = mapped_planning_path({**device, "control_type": control_type}, saved if isinstance(saved, dict) else {},
                                    options.get("pool_water_temperature_entity"))
        if path == "pool":
            saved = pool_mapping(saved, control_type)
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
                "method_sync_pending": device.get("method_sync_pending", False),
                "mapping": saved_mapping,
                "meter_room": device.get("meter_room"),
                "stale_mapping_control_type": (
                    saved.get("control_type")
                    if isinstance(saved, dict)
                    and saved.get("control_type") != control_type
                    else None
                ),
                "suggested_mapping": (suggest_device_control_mapping(
                    catalog, device, control_type
                ) if include_suggestions else {}),
                "execution_status": coordinator.controller.status.get("device:" + device["key"]),
                "fields": _control_fields({**device, "control_type": control_type, "planning_system": path}),
                **report,
            }
        )


    views = complete_device_views(devices, options, choices, coordinator.operational_status,
        coordinator.optimisation_plan or {}, coordinator.controller.status, entity_names,
        area_names, datetime.now(timezone.utc), explicit.keys())
    for row in views:
        location = (row.get('meter_room') or {}).get('area_name')
        row.update(room_name=location or 'No room', equipment_area=location)
    locate_pool(views)
    return views


def locate_pool(devices):
    """Place shared pool equipment where its dashboard devices agree."""
    pool = [row for row in devices if row['planned'] and row['permission']['controller_id'] == 'pool']
    locations = {row['equipment_area'] for row in pool}
    if len(locations) > 1:
        for row in pool:
            row['equipment_area'] = None

