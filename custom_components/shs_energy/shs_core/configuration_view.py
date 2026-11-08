"""Shared configuration read model owned by the app."""
from datetime import datetime, timezone
from . import const as shs_const
from .configuration_schema import resolve_configuration
from .configuration_fields import _control_fields
from .discovery import suggest_device_control_mapping
from .device_commands import actuator_targets
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
        control_type = str(device.get("control_type") or (mappings.get(device["key"]) or {}).get("control_type") or "")
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
                "mapping": saved_mapping,
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
    locate_pool(views, area_names, entity_area_ids)
    return views


def locate_pool(devices, names, areas):
    """Locate the shared system by its meters, which can be remote from its controls."""
    pool = [row for row in devices if row['planned'] and row['permission']['controller_id'] == 'pool']
    meters, controls = set(), set()
    for row in pool:
        mapping = row['mapping']
        sources = (row['statistic_id'], mapping.get('power'))
        meters.update(areas[e] for e in sources if isinstance(e, str) and e in areas)
        controls.update(areas[e] for e in actuator_targets(mapping) if e in areas)
    locations = meters or controls
    if len(locations) == 1 and (name := names.get(next(iter(locations)))):
        for row in pool:
            row.update(room_name=name, equipment_area=name)

