"""Full-page Home Assistant configuration surface for SHS Energy."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import const as shs_const
from .api import ShsApiError
from .api_contract import INTEGRATION_VERSION
from .configuration import (
    area_name_by_id,
    async_discover_configuration,
    entity_area_id,
    entity_area_id_by_id,
    entity_display_name_by_id,
    resolved_options,
    suggest_device_control_mapping,
)
from .configuration_fields import _control_fields, _configuration_sections, LABELS
from .presentation import complete_device_views, timeline, system_fields, device_name, device_readiness
from .configuration_schema import (
    prepare_options, save_device,
)
from .device_controls import (
    apply_planner_support,
    is_room_thermal_control,
    mapping_report,
)

_LOGGER = logging.getLogger(__name__)

PANEL_URL = "shs-energy"
STATIC_URL = "/shs_energy_frontend"
FRONTEND_DIR = Path(__file__).parent / "frontend"
FRONTEND_ASSET_VERSION = INTEGRATION_VERSION
# Custom elements cannot be redefined in an open browser session. Version the
# element as well as the module URL so an update cannot reuse the old class.
PANEL_ELEMENT = f"shs-energy-config-panel-{FRONTEND_ASSET_VERSION.replace('.', '-')}"


def _entry_state(entry: ConfigEntry) -> str:
    return str(getattr(entry.state, "value", entry.state))


def _entry_from_message(
    hass: HomeAssistant, entry_id: str | None
) -> ConfigEntry | None:
    entries = hass.config_entries.async_entries(shs_const.DOMAIN)
    if entry_id:
        entry = hass.config_entries.async_get_entry(entry_id)
        return entry if entry is not None and entry.domain == shs_const.DOMAIN else None
    return entries[0] if len(entries) == 1 else None


def _entries(hass: HomeAssistant) -> list[dict[str, str]]:
    return [
        {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "state": _entry_state(entry),
        }
        for entry in hass.config_entries.async_entries(shs_const.DOMAIN)
    ]


def _entity_catalog(hass: HomeAssistant) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    area_names = area_name_by_id(hass)
    for state in sorted(hass.states.async_all(), key=lambda value: value.entity_id):
        area_id = entity_area_id(hass, state.entity_id)
        result.append(
            {
                "entity_id": state.entity_id,
                "name": str(state.attributes.get("friendly_name") or state.entity_id),
                "domain": state.entity_id.split(".", 1)[0],
                "state": str(state.state)[:120],
                "unit": state.attributes.get("unit_of_measurement"),
                "last_updated": state.last_updated.isoformat(),
                "device_class": state.attributes.get("device_class"),
                "minimum": state.attributes.get("min"),
                "maximum": state.attributes.get("max"),
                "area_id": area_id,
                "area_name": area_names.get(area_id) if area_id else None,
            }
        )
    return result


def _mapping_suggestions(
    hass: HomeAssistant,
    device: dict[str, Any],
    control_type: str,
) -> dict[str, Any]:
    """Return live Home Assistant suggestions for the current card contract."""
    return suggest_device_control_mapping(hass, device, control_type)


async def _configuration_payload(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    refresh_roles: bool,
) -> dict[str, Any]:
    coordinator = entry.runtime_data
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
    options = resolved_options(hass, dict(entry.options))
    exchange_status = await coordinator.async_cached_exchange_status()
    mappings = options.get(shs_const.OPT_DEVICE_CONTROL_MAPPINGS, {})
    if not isinstance(mappings, dict):
        mappings = {}
    known_entity_ids = {state.entity_id for state in hass.states.async_all()}
    entity_names = entity_display_name_by_id(hass)
    area_names = area_name_by_id(hass)
    entity_area_ids = entity_area_id_by_id(hass)
    devices: list[dict[str, Any]] = []
    active_commands = (coordinator.current_plan_slot or {}).get("device_commands", {})
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
                room_control=is_room_thermal_control(
                    control_type, str(device.get("category") or "")
                ),
            ),
            control_type,
            str(device.get("category") or ""),
        )
        command = active_commands.get(device["key"])
        execution_reason = (command.get("reason") if command and command.get("type") == "unavailable"
                            else None if command else "Awaiting an executable schema-7 plan from the website")
        if control_type not in {"setpoint", "switch_schedule", "permit_inhibit", "variable_power"}:
            execution_reason = f"The {control_type} method selected on the website is not supported by this integration"
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
                "suggested_mapping": _mapping_suggestions(
                    hass, device, control_type
                ),
                "execution_reason": execution_reason,
                "execution_status": coordinator.controller.status.get("device:" + device["key"]),
                "fields": list(_control_fields({**device, "control_type": control_type})),
                **report,
            }
        )

    thermal_devices = [
        device
        for device in devices
        if device["planning_role"] == "controllable" and is_room_thermal_control(device["control_type"], device.get("category"))
        and (
            device["control_type"] == "setpoint"
            or bool(device.get("mapping", {}).get("temperature_entity_id"))
        )
    ]
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
    devices = complete_device_views(devices, options, choices, operation, plan,
        coordinator.controller.status, entity_names, area_names, datetime.now(timezone.utc), entry.options.keys())
    for device in devices:
        mapping = device.get("mapping", {})
        source_ids = [mapping.get("temperature_entity_id"), mapping.get("power")]
        if device.get("system"):
            system = device["system"]
            source_ids.extend([options.get(system + "_soc_entity"), options.get(system + "_water_temperature_entity")])
        device["readings"] = []
        for entity_id in dict.fromkeys(value for value in source_ids if isinstance(value, str)):
            state = hass.states.get(entity_id)
            if state:
                device["readings"].append({"name": state.attributes.get("friendly_name") or "Reading",
                    "value": state.state, "unit": state.attributes.get("unit_of_measurement", ""),
                    "updated_at": state.last_updated.isoformat()})
    return {
        "labels": LABELS,
        "operation": operation,
        "timeline": timeline(plan, operation),
        "website_url": shs_const.website_url(entry.data[shs_const.CONF_BASE_URL], "/portal/energy-modeling?tab=devices"),
        "configured_keys": list(entry.options),
        "meter_inventory": [{"key": d["key"], "name": device_name(entity_names.get(d["key"]) or d["name"])} for d in requested],
        "entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "state": _entry_state(entry),
        },
        "configuration": options,
        "sections": _configuration_sections(),
        "entities": _entity_catalog(hass),
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
                            entry.data[shs_const.CONF_BASE_URL],
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
            "last_thermal_slots_accepted": thermal_slots,
            "thermal_slots_accepted_until": thermal_accepted_until,
        },
    }


def _read_entity(hass, entity_id):
    state = hass.states.get(entity_id)
    return {"state": state.state, "attributes": dict(state.attributes)} if state else None


async def async_apply_configuration(
    hass: HomeAssistant,
    entry: ConfigEntry,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Validate and persist a complete or partial non-device update."""
    if shs_const.OPT_DEVICE_CONTROL_MAPPINGS in incoming:
        raise ValueError("device mappings must be saved from their own card")
    options = prepare_options(
        dict(entry.options), incoming, lambda entity: _read_entity(hass, entity),
        latitude=hass.config.latitude, longitude=hass.config.longitude,
    )
    if options.get(shs_const.OPT_PLANNING_MODE) == shs_const.PLANNING_MODE_LIVE:
        options[shs_const.OPT_CONFIGURATION_REVIEWED_AT] = datetime.now(
            timezone.utc
        ).isoformat()
    hass.config_entries.async_update_entry(entry, options=options)
    return options


async def async_apply_device_mapping(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_key: str,
    incoming: dict[str, Any] | None,
    configuration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate, report and save a device and its shared room observation."""
    choices = await entry.runtime_data.async_cached_planning_configuration()
    requested = choices["devices"]
    device = next((item for item in requested if item["key"] == device_key), None)
    if device is None:
        if device_key not in {"$battery", "$ev", "$pool"}:
            raise ValueError("This device is no longer in the website inventory")
        if incoming:
            raise ValueError("This equipment has no device meter to configure")
        system = device_key[1:]
        if set(configuration or {}) - {field["key"] for field in system_fields(system)}:
            raise ValueError("These settings belong to another device")
        await async_apply_configuration(hass, entry, configuration or {})
        return {"mapping_status": "ready", "mapping_error": None, "mapping_summary": {}}


    if device.get("planning_role") != "controllable":
        saved = resolved_options(hass, dict(entry.options)).get("device_control_mappings", {}).get(device_key, {})
        device = {**device, "control_type": saved.get("control_type")}
        if incoming and incoming.get("control_enabled") and not saved.get("control_enabled"):
            raise ValueError("Include this device on the website before enabling control")
    if incoming and incoming.get("control_enabled") and device.get("planning_role") == "controllable":
        command = (entry.runtime_data.current_plan_slot or {}).get("device_commands", {}).get(device_key)
        if not command or command.get("type") == "unavailable":
            raise ValueError("Device control requires an executable schema-7 plan from the website")
    existing = dict(entry.options)
    if configuration:
        panel = await _configuration_payload(hass, entry, refresh_roles=False)
        view = next((item for item in panel["devices"] if item["key"] == device_key), None)
        allowed = {field["key"] for field in (view or {}).get("system_fields", [])}
        if set(configuration) - allowed:
            raise ValueError("These settings belong to another device")
        existing = prepare_options(existing, configuration, lambda entity: _read_entity(hass, entity),
                                   latitude=hass.config.latitude, longitude=hass.config.longitude)
    options = save_device(
        existing, device_key, incoming, device,
        lambda entity: _read_entity(hass, entity),
        entity_names=entity_display_name_by_id(hass),
        area_names=area_name_by_id(hass), entity_area_ids=entity_area_id_by_id(hass),
    )
    mappings = resolved_options(hass, options)[shs_const.OPT_DEVICE_CONTROL_MAPPINGS]
    report = await entry.runtime_data.async_report_device_mapping(device_key, mappings)
    if options.get(shs_const.OPT_PLANNING_MODE) == shs_const.PLANNING_MODE_LIVE:
        options[shs_const.OPT_CONFIGURATION_REVIEWED_AT] = datetime.now(
            timezone.utc
        ).isoformat()
    hass.config_entries.async_update_entry(entry, options=options)
    # The saved mapping's status is already in `report`; the replan only
    # refreshes the plan behind it. Awaiting it here made Save sit for the
    # length of a full statistics sweep before the card could answer.
    entry.async_create_background_task(
        hass,
        entry.runtime_data.async_optimisation_push(force_plan=True),
        name=f"{shs_const.DOMAIN}_replan_after_mapping_save",
    )
    return report


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{shs_const.DOMAIN}/config/get",
        vol.Optional("config_entry"): str,
        vol.Optional("refresh_roles", default=True): bool,
    }
)
@websocket_api.async_response
async def websocket_get_configuration(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the complete local configuration and current website request."""
    entry = _entry_from_message(hass, msg.get("config_entry"))
    if entry is None:
        connection.send_result(
            msg["id"],
            {"requires_entry_selection": True, "entries": _entries(hass)},
        )
        return
    if _entry_state(entry) != "loaded":
        connection.send_error(msg["id"], "not_loaded", "The integration is not loaded")
        return
    try:
        payload = await _configuration_payload(
            hass, entry, refresh_roles=bool(msg["refresh_roles"])
        )
    except Exception as err:  # Home Assistant turns this into one visible panel error.
        _LOGGER.exception("Unable to build SHS Energy configuration panel data")
        connection.send_error(msg["id"], "configuration_error", str(err))
        return
    connection.send_result(msg["id"], payload)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{shs_const.DOMAIN}/config/discover",
        vol.Required("config_entry"): str,
    }
)
@websocket_api.async_response
async def websocket_discover_configuration(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Build an Energy Dashboard proposal without saving it."""
    entry = _entry_from_message(hass, msg["config_entry"])
    if entry is None:
        connection.send_error(msg["id"], "not_found", "SHS Energy entry not found")
        return
    try:
        discovery = await async_discover_configuration(hass, dict(entry.options))
    except Exception as err:
        _LOGGER.exception("SHS Energy automatic discovery failed")
        connection.send_error(msg["id"], "discovery_error", str(err))
        return
    connection.send_result(msg["id"], discovery)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{shs_const.DOMAIN}/config/save",
        vol.Required("config_entry"): str,
        vol.Required("configuration"): dict,
    }
)
@websocket_api.async_response
async def websocket_save_configuration(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Validate and save the panel draft."""
    entry = _entry_from_message(hass, msg["config_entry"])
    if entry is None:
        connection.send_error(msg["id"], "not_found", "SHS Energy entry not found")
        return
    try:
        options = await async_apply_configuration(
            hass, entry, dict(msg["configuration"])
        )
    except (TypeError, ValueError) as err:
        connection.send_error(msg["id"], "invalid_configuration", str(err))
        return
    connection.send_result(
        msg["id"],
        {
            "saved": True,
            "configuration_reviewed_at": options.get(
                shs_const.OPT_CONFIGURATION_REVIEWED_AT
            ),
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{shs_const.DOMAIN}/config/save_device",
        vol.Required("config_entry"): str,
        vol.Required("device_key"): str,
        vol.Required("mapping"): vol.Any(dict, None),
        vol.Optional("configuration", default={}): dict,
    }
)
@websocket_api.async_response
async def websocket_save_device_configuration(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Save and report one local control mapping."""
    entry = _entry_from_message(hass, msg["config_entry"])
    if entry is None:
        connection.send_error(msg["id"], "not_found", "SHS Energy entry not found")
        return
    try:
        report = await async_apply_device_mapping(
            hass,
            entry,
            msg["device_key"],
            dict(msg["mapping"]) if msg["mapping"] is not None else None,
            msg["configuration"],
        )
        panel = await _configuration_payload(hass, entry, refresh_roles=False)
    except (ShsApiError, TypeError, ValueError) as err:
        connection.send_error(msg["id"], "invalid_device_mapping", str(err))
        return
    connection.send_result(msg["id"], {"saved": True, **report, "panel": panel})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{shs_const.DOMAIN}/status/get", vol.Required("config_entry"): str})
@websocket_api.async_response
async def websocket_get_status(hass, connection, msg):
    entry = _entry_from_message(hass, msg["config_entry"])
    if entry is None or _entry_state(entry) != "loaded":
        connection.send_error(msg["id"], "not_loaded", "The integration is not loaded")
        return
    coordinator = entry.runtime_data
    status = coordinator.operational_status
    connection.send_result(msg["id"], {"operation": status,
        "timeline": timeline(coordinator.optimisation_plan, status),
        "controllers": dict(coordinator.controller.status)})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{shs_const.DOMAIN}/config/control", vol.Required("config_entry"): str,
    vol.Required("device_key"): str, vol.Required("enabled"): bool})
@websocket_api.async_response
async def websocket_control_permission(hass, connection, msg):
    entry = _entry_from_message(hass, msg["config_entry"])
    if entry is None or _entry_state(entry) != "loaded":
        connection.send_error(msg["id"], "not_loaded", "The integration is not loaded")
        return
    try:
        panel = await _configuration_payload(hass, entry, refresh_roles=False)
        device = next((d for d in panel["devices"] if d["key"] == msg["device_key"]), None)
        if device is None:
            raise ValueError("This equipment is no longer present")
        if msg["enabled"] and device["permission"]["reason"]:
            raise ValueError(device["permission"]["reason"])
        if system := device.get("system"):
            await async_apply_configuration(hass, entry, {system + "_control_enabled": msg["enabled"]})
        else:
            mapping = dict(panel["configuration"].get("device_control_mappings", {}).get(device["key"], {}))
            if not mapping:
                raise ValueError("Set up this device first")
            mapping["control_enabled"] = msg["enabled"]
            await async_apply_device_mapping(hass, entry, device["key"], mapping)
        await entry.runtime_data.controller.async_tick()
        connection.send_result(msg["id"], await _configuration_payload(hass, entry, refresh_roles=False))
    except (ShsApiError, ValueError, TypeError) as err:
        connection.send_error(msg["id"], "control_permission_failed", str(err))


async def async_register_config_panel(hass: HomeAssistant) -> None:
    """Register the static bundle, backend commands and cogwheel destination."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(FRONTEND_DIR), False)]
    )
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name=PANEL_ELEMENT,
        module_url=(
            f"{STATIC_URL}/shs-energy-config-panel.js"
            f"?v={FRONTEND_ASSET_VERSION}"
        ),
        sidebar_title=None,
        sidebar_icon="mdi:home-lightning-bolt",
        config={"domain": shs_const.DOMAIN},
        require_admin=True,
        config_panel_domain=shs_const.DOMAIN,
    )
    websocket_api.async_register_command(hass, websocket_get_status)
    websocket_api.async_register_command(hass, websocket_control_permission)
    websocket_api.async_register_command(hass, websocket_get_configuration)
    websocket_api.async_register_command(hass, websocket_discover_configuration)
    websocket_api.async_register_command(hass, websocket_save_configuration)
    websocket_api.async_register_command(hass, websocket_save_device_configuration)
