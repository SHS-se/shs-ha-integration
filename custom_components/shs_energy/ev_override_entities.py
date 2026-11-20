"""Home Assistant controls for the manual EV charge override (docs/ev-charge-override.md)."""
from datetime import timedelta
from math import ceil

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

from .equipment import equipment_device_info
from .shs_core.gateway_journal import GatewayConflict
from .shs_core.operating_modes import device_mode


def ev_device_info(entry):
    rows = (entry.runtime_data.catalogue or {}).get('devices', []) if entry.runtime_data.catalogue else []
    row = next((row for row in rows if row['permission']['controller_id'] == 'ev'
                and not row.get('system_member')), {})
    return equipment_device_info(entry, 'ev', row.get('name', 'Vehicle charger'), area=row.get('equipment_area'))


class EvOverrideEntity:
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry, field):
        self.entry, self.field = entry, field
        self._attr_unique_id = f'{entry.entry_id}_ev_override_{field}'
        self._attr_translation_key = f'ev_override_{field}'
        self._attr_device_info = ev_device_info(entry)

    @property
    def service(self):
        return self.entry.runtime_data.service

    @property
    def available(self):
        service = self.service
        if not self.entry.runtime_data.online or service is None or service.ev_override is None:
            return False
        try:
            return device_mode(service.source.options(), 'ev') == 'controlling'
        except (AttributeError, KeyError, TypeError):
            return False

    async def change(self, value):
        try:
            await self.service.change_ev_override(self.field, value)
        except (GatewayConflict, ValueError, TypeError) as error:
            raise HomeAssistantError(str(error)) from error

    def readings(self):
        return self.service.ev_override_inputs()

    @property
    def extra_state_attributes(self):
        if not self.available:
            return None
        store = self.service.ev_override
        row = store.active
        soc = self.readings()['soc']
        return {'active': row is not None, 'current_soc': None if soc is None else round(soc * 100, 1),
                'target_soc': row['target_soc'] if row else None,
                'deadline': row.get('deadline') if row else None,
                'saved_charge_limit': row.get('saved_limit') if row else None,
                'preset_hours': store.value['hours']}

    def display_target(self):
        row = self.service.ev_override.active
        if row is not None:
            return row['target_soc']
        soc = self.readings()['soc']
        return None if soc is None else round(soc * 100)

    def display_hours(self):
        store = self.service.ev_override
        row = store.active
        if row is None:
            return store.value['hours'] or 0
        fastest = self.readings()['fastest'](row['target_soc'] / 100)
        deadline = store.deadline()
        if deadline is None:
            return fastest
        left = ceil(max(0.0, (deadline - store.now()).total_seconds()) / 3600)
        return max(left, fastest)


async def setup_ev_override_entities(hass, entry, make, async_add_entities):
    """Show the override beside the car charger, refreshed as the car reports."""
    coordinator = entry.runtime_data
    entities = make(entry)

    @callback
    def refresh(_event=None):
        for entity in entities:
            if entity.hass is not None:
                entity.async_write_ha_state()

    async_add_entities(entities)
    entry.async_on_unload(coordinator.async_add_listener(refresh))
    entry.async_on_unload(async_track_time_interval(hass, refresh, timedelta(minutes=1)))
    service = coordinator.service
    tracked = [] if service is None else [entity for key in ('ev_soc_entity', 'ev_connected_entity')
                                          if (entity := service.source.options().get(key))]
    if tracked:
        entry.async_on_unload(async_track_state_change_event(hass, tracked, refresh))
