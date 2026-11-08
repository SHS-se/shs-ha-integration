"""Home Assistant device identity shared by all SHS equipment entities."""
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo

from .shs_core.const import CONF_DEVICE_TOKEN_ID, DOMAIN


def equipment_identifier(entry, owner):
    return DOMAIN, f'{entry.data[CONF_DEVICE_TOKEN_ID]}:heating:{owner}'


def equipment_device_info(entry, owner, name, *, area=None):
    info = DeviceInfo(identifiers={equipment_identifier(entry, owner)},
        name=name, manufacturer='Smart Home Solutions')
    if area:
        info['suggested_area'] = area
    return info


def equipment_device(hass, entry, owner):
    identifier = equipment_identifier(entry, owner)
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return next((device for device in devices if identifier in device.identifiers), None)


def assign_equipment_area(hass, device, area_name):
    if device and not device.area_id and area_name:
        area = ar.async_get(hass).async_get_area_by_name(area_name)
        if area:
            dr.async_get(hass).async_update_device(device.id, area_id=area.id)


def update_device_info(hass, entry, domain, entity, info):
    """Reattach an existing entity if its catalogue assignment changes."""
    if entity._attr_device_info == info:
        return
    entity._attr_device_info = info
    entity.__dict__.pop('device_info', None)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(domain, DOMAIN, entity.unique_id)
    if entity_id is not None:
        device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, **info)
        if info.get('suggested_area'):
            assign_equipment_area(hass, device, info['suggested_area'])
        registry.async_update_entity(entity_id, device_id=device.id)


def remove_empty_heater_device(hass, entry, owner):
    """Retire the old SHS device page only after its registered entities move."""
    device = equipment_device(hass, entry, owner)
    if device is not None and not er.async_entries_for_device(
            er.async_get(hass), device.id, include_disabled_entities=True):
        dr.async_get(hass).async_remove_device(device.id)
