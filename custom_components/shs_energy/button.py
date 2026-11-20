"""End an SHS heating or charge override and reevaluate the current plan."""
from homeassistant.components.button import ButtonEntity

from .heating_entities import HeatingEntity, setup_heating_entities
from .ev_override_entities import EvOverrideEntity, setup_ev_override_entities


class ResumeHeating(HeatingEntity, ButtonEntity):
    _attr_translation_key = 'heating_resume'

    async def async_press(self):
        await self.change(None)


class EndChargeOverride(EvOverrideEntity, ButtonEntity):
    async def async_press(self):
        await self.change(None)


async def async_setup_entry(hass, entry, async_add_entities):
    await setup_heating_entities(hass, entry, lambda entry, owner, row: [
        ResumeHeating(entry, owner, row, 'resume')])
    await setup_ev_override_entities(hass, entry, lambda entry: [EndChargeOverride(entry, 'end')],
                                     async_add_entities)
