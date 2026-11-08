"""Home Assistant controls projected from the gateway's room instructions."""
from datetime import timedelta
from math import ceil

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import async_get_current_platform
from homeassistant.helpers.event import async_track_time_interval

from .equipment import equipment_device, equipment_device_info, equipment_identifier
from .shs_core.const import DOMAIN
from .shs_core.gateway_journal import GatewayConflict


class HeatingEntity:
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry, owner, row, field):
        self.entry, self.owner, self.row, self.field = entry, owner, row, field
        self._attr_unique_id = f"{entry.entry_id}_heating_{row.get('entity_owner', owner)}_{field}"
        self._attr_device_info = equipment_device_info(entry, owner, row['name'], area=row.get('area_name'))

    @property
    def available(self):
        return self.entry.runtime_data.online and self.row['controlling']

    async def change(self, value):
        try:
            await self.entry.runtime_data.service.change_heating(
                dict(owner=self.owner, field=self.field, value=value))
        except (GatewayConflict, ValueError, TypeError) as error:
            raise HomeAssistantError(str(error)) from error

    @property
    def minutes(self):
        from datetime import datetime
        end = self.row['expires_at']
        now = self.entry.runtime_data.service.heating.now()
        return max(0, ceil((datetime.fromisoformat(end) - now).total_seconds() / 60)) if end else self.row['minutes']


def registered_room_controls(hass, entry, owner, row):
    """Keep the room's registered controls, including the earlier thermostat IDs."""
    prefix = f'{entry.entry_id}_heating_'
    registry = er.async_get(hass)
    device = equipment_device(hass, entry, owner)
    if device is not None:
        candidates = sorted(
            item.unique_id.removeprefix(prefix).removesuffix('_target')
            for item in er.async_entries_for_device(registry, device.id, include_disabled_entities=True)
            if item.platform == DOMAIN and item.unique_id.startswith(prefix)
            and item.unique_id.endswith('_target'))
        if candidates:
            return candidates[0]
    for prior in sorted(row['equipment_targets']):
        entity_id = registry.async_get_entity_id('number', DOMAIN, f'{prefix}{prior}_target')
        if entity_id is None:
            continue
        entity = registry.async_get(entity_id)
        old = dr.async_get(hass).async_get(entity.device_id) if entity.device_id else None
        if old is not None and equipment_identifier(entry, prior) in old.identifiers:
            return prior
    return owner


async def setup_heating_entities(hass, entry, make):
    """Use one lifecycle for the two writable numbers and the resume button."""
    coordinator = entry.runtime_data
    platform = async_get_current_platform()
    entities = {}
    task = None
    dirty = False

    async def refresh():
        nonlocal dirty
        while dirty:
            dirty = False
            if not coordinator.online:
                for group in entities.values():
                    for entity in group:
                        entity.async_write_ha_state()
                continue
            zones = coordinator.service.heating_controls()['zones']
            for owner in entities.keys() - zones.keys():
                for entity in entities.pop(owner):
                    await entity.async_remove(force_remove=True)
            additions = []
            for owner, row in zones.items():
                if owner not in entities:
                    if row.get('area_id'):
                        row = {**row, 'entity_owner': registered_room_controls(hass, entry, owner, row)}
                    entities[owner] = make(entry, owner, row)
                    additions.extend(entities[owner])
                else:
                    for entity in entities[owner]:
                        entity.row = row
                        entity.async_write_ha_state()
            if additions:
                await platform.async_add_entities(additions)

    @callback
    def schedule(_now=None):
        nonlocal task, dirty
        dirty = True
        if task is None or task.done():
            task = entry.async_create_background_task(hass, refresh(), name='shs_heating_entities')

    def stop():
        if task is not None:
            task.cancel()

    entry.async_on_unload(stop)
    entry.async_on_unload(coordinator.async_add_listener(schedule))
    entry.async_on_unload(async_track_time_interval(hass, schedule, timedelta(minutes=1)))
    dirty = True
    await refresh()
