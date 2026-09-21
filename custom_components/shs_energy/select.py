"""Execution permissions for exactly the devices in the Schedule tab."""
from __future__ import annotations

import asyncio
from homeassistant.components.select import SelectEntity
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import async_get_current_platform

from .const import DOMAIN, CONF_CUSTOMER_NAME, CONF_DEVICE_TOKEN_ID
from .control_configuration import async_execution_devices, async_set_execution_mode
from .operating_modes import MODES, device_mode
from .presentation import controller_explanation


async def async_setup_entry(hass, entry, async_add_entities):
    platform = async_get_current_platform()
    entities = ExecutionModeEntities(hass, entry, platform.async_add_entities)
    entry.async_on_unload(entities.close)
    entry.async_on_unload(entry.runtime_data.async_add_listener(entities.schedule_refresh))
    entry.async_on_unload(entry.add_update_listener(entities.options_updated))
    await entities.refresh()


class ExecutionModeEntities:
    """Serialize inventory changes, including removal of stale registry entries."""
    def __init__(self, hass, entry, add_entities):
        self.hass, self.entry, self.add_entities = hass, entry, add_entities
        self.entities = {}
        self.prefix = f'{entry.entry_id}_execution_mode_'
        self.lock = asyncio.Lock()
        self.task = None
        self.dirty = False
        self.closed = False

    @callback
    def close(self):
        self.closed = True
        if self.task is not None:
            self.task.cancel()

    @callback
    def schedule_refresh(self):
        if self.closed:
            return
        self.dirty = True
        if self.task is None or self.task.done():
            self.task = self.entry.async_create_background_task(
                self.hass, self.refresh(), name=f'{DOMAIN}_execution_mode_entities')

    async def options_updated(self, hass, entry):
        self.schedule_refresh()

    async def refresh(self):
        async with self.lock:
            while not self.closed:
                self.dirty = False
                # Native selects use reviewed mappings and permissions, not the
                # editor's semantic search over every entity for suggestions.
                devices = await async_execution_devices(self.hass, self.entry, include_suggestions=False)
                if self.closed:
                    return
                # A system member shares its owner's grant, and so its select.
                wanted = {d['key']: d for d in devices if d['planned'] and not d.get('system_member')}
                registry = er.async_get(self.hass)
                for key in set(self.entities) - wanted.keys():
                    entity = self.entities.pop(key)
                    if entity.active:
                        await entity.async_remove(force_remove=True)
                wanted_ids = {self.prefix + key for key in wanted}
                for registered in er.async_entries_for_config_entry(registry, self.entry.entry_id):
                    if (registered.domain == 'select' and registered.platform == DOMAIN
                            and registered.unique_id.startswith(self.prefix)
                            and registered.unique_id not in wanted_ids):
                        registry.async_remove(registered.entity_id)
                additions = []
                for key, device in wanted.items():
                    if key in self.entities:
                        self.entities[key].update_device(device)
                    else:
                        entity = ExecutionModeSelect(self.entry, device, self.prefix + key)
                        self.entities[key] = entity
                        additions.append(entity)
                if additions:
                    await self.add_entities(additions)
                if not self.dirty:
                    return


class ExecutionModeSelect(SelectEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = 'execution_mode'
    _attr_options = list(MODES)
    _attr_icon = 'mdi:robot'

    def __init__(self, entry, device, unique_id):
        self.entry = entry
        self.active = False
        self._unsubscribe_battery = None
        self.device = device
        self._attr_unique_id = unique_id
        self._attr_translation_placeholders = {'device': device['name']}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_TOKEN_ID])},
            name=entry.data.get(CONF_CUSTOMER_NAME) or 'Smart Home Solutions',
            manufacturer='Smart Home Solutions', entry_type=DeviceEntryType.SERVICE)

    async def async_added_to_hass(self):
        self.active = True
        self._unsubscribe_battery = self.entry.runtime_data.async_add_battery_listener(self._battery_updated)

    async def async_will_remove_from_hass(self):
        self.active = False
        if self._unsubscribe_battery is not None:
            self._unsubscribe_battery()
            self._unsubscribe_battery = None

    @callback
    def _battery_updated(self):
        # Battery details change every refresh; other owners change with the plan.
        if self.active and self.device['permission']['controller_id'] == 'battery':
            self.async_write_ha_state()

    @property
    def current_option(self):
        mode = device_mode(self.entry.options, self.device['permission']['controller_id'])
        return mode if mode in MODES else None

    @property
    def extra_state_attributes(self):
        coordinator = self.entry.runtime_data
        owner = self.device['permission']['controller_id']
        status = coordinator.controller.status.get(owner, {})
        if owner == 'battery':
            status = {**status, 'battery_runtime': coordinator.battery_runtime.snapshot()}
        _, slot = coordinator.binding_plan_for(owner, coordinator.controller.options())
        return {'device_key': self.device['key'],
                'controlling_blocked_reason': self.device['permission']['reason'],
                **controller_explanation(owner, self.current_option, status, slot)}

    @callback
    def update_device(self, device):
        self.device = device
        self._attr_translation_placeholders = {'device': device['name']}
        if self.active:
            self.async_write_ha_state()

    async def async_select_option(self, option):
        try:
            await async_set_execution_mode(self.hass, self.entry, self.device['key'], option)
        except (ValueError, TypeError) as error:
            raise HomeAssistantError(str(error)) from error
        self.async_write_ha_state()
