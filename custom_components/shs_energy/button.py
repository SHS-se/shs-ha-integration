"""End an SHS heating override and reevaluate the current plan."""
from homeassistant.components.button import ButtonEntity

from .heating_entities import HeatingEntity, setup_heating_entities


class ResumeHeating(HeatingEntity, ButtonEntity):
    _attr_translation_key = 'heating_resume'

    async def async_press(self):
        await self.change(None)


async def async_setup_entry(hass, entry, async_add_entities):
    await setup_heating_entities(hass, entry, lambda entry, owner, row: [
        ResumeHeating(entry, owner, row, 'resume')])
