"""Home Assistant registry metadata and native configuration defaults."""
from __future__ import annotations
from typing import Any
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from .shs_core.configuration_schema import configuration_defaults, resolve_configuration

def optimisation_defaults(hass: HomeAssistant) -> dict[str, Any]:
    return configuration_defaults(hass.config.latitude, hass.config.longitude)


def resolved_options(hass: HomeAssistant, options: dict[str, Any]) -> dict[str, Any]:
    return resolve_configuration(options, hass.config.latitude, hass.config.longitude)


def area_name_by_id(hass: HomeAssistant) -> dict[str, str]:
    """Return Home Assistant's stable area ids and user-facing room names."""
    registry = ar.async_get(hass)
    return {area.id: area.name for area in registry.async_list_areas()}


def entity_area_id(hass: HomeAssistant, entity_id: str) -> str | None:
    """Resolve an entity's own area, then its device's area."""
    entity = er.async_get(hass).async_get(entity_id)
    if entity is None:
        return None
    if entity.area_id:
        return entity.area_id
    if not entity.device_id:
        return None
    device = dr.async_get(hass).async_get(entity.device_id)
    return device.area_id if device is not None else None


def entity_area_id_by_id(hass: HomeAssistant) -> dict[str, str]:
    """Resolve every live entity to its own or parent device area."""
    result: dict[str, str] = {}
    for state in hass.states.async_all():
        area_id = entity_area_id(hass, state.entity_id)
        if area_id:
            result[state.entity_id] = area_id
    return result


def entity_display_name_by_id(hass: HomeAssistant) -> dict[str, str]:
    """Name controlled hardware without exposing entity ids to the website."""
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    result: dict[str, str] = {}
    for state in hass.states.async_all():
        entity = entities.async_get(state.entity_id)
        device = (
            devices.async_get(entity.device_id)
            if entity is not None and entity.device_id
            else None
        )
        result[state.entity_id] = str(
            (device.name_by_user if device else None)
            or (device.name if device else None)
            or state.attributes.get("friendly_name")
            or state.entity_id
        )
    return result



