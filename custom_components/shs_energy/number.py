"""Writable setpoint and time controls for each SHS heating zone."""
from datetime import datetime, timedelta

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode

from .heating_entities import HeatingEntity, setup_heating_entities


class HeatingNumber(HeatingEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(self, entry, owner, row, field):
        super().__init__(entry, owner, row, field)
        self._attr_translation_key = 'heating_target' if field == 'target' else 'heating_minutes'
        self._attr_device_class = NumberDeviceClass.TEMPERATURE if field == 'target' else NumberDeviceClass.DURATION
        self._attr_native_unit_of_measurement = '°C' if field == 'target' else 'min'

    @property
    def available(self):
        return super().available and (self.field != 'target'
            or bool(self.row.get('relay_states'))
            or self.row['min_c'] is not None and self.row['max_c'] is not None)

    @property
    def native_value(self):
        if self.field != 'target':
            return self.minutes
        room_target = self.row.get('room_target_c')
        return self.row['applied_c'] if room_target is None else room_target

    @property
    def native_min_value(self):
        if self.field != 'target':
            return 1
        minimum = self.row['min_c']
        return minimum if minimum is not None else super().native_min_value

    @property
    def native_max_value(self):
        if self.field == 'target':
            maximum = self.row['max_c']
            return maximum if maximum is not None else super().native_max_value
        now = self.entry.runtime_data.service.heating.now()
        return (datetime.max.replace(tzinfo=now.tzinfo) - now) // timedelta(minutes=1)

    @property
    def native_step(self):
        return self.row['step'] if self.field == 'target' else 1

    @property
    def extra_state_attributes(self):
        return {key: self.row.get(key) for key in ('planned_target_c', 'applied_c', 'expires_at',
                                                    'relay_states', 'equipment_targets')}

    async def async_set_native_value(self, value):
        await self.change(value)


async def async_setup_entry(hass, entry, async_add_entities):
    await setup_heating_entities(hass, entry, lambda entry, owner, row: [
        HeatingNumber(entry, owner, row, field) for field in ('target', 'minutes')])
