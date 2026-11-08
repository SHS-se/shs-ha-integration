"""Home Assistant device identity shared by all SHS equipment entities."""
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo

from .shs_core.const import CONF_DEVICE_TOKEN_ID, DOMAIN


def equipment_device_info(entry, owner, name):
    # Keep the existing floor-heater device IDs when their other entities join.
    return DeviceInfo(identifiers={(DOMAIN, f'{entry.data[CONF_DEVICE_TOKEN_ID]}:heating:{owner}')},
        name=name, manufacturer='Smart Home Solutions')


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
        registry.async_update_entity(entity_id, device_id=device.id)
