"""Read-only Home Assistant history and forecast adapters."""
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.exceptions import HomeAssistantError
from .shs_core.household_ports import HouseholdReadError

class RecorderSource:
    """Read source rows; aggregation and planning belong to the household."""

    def __init__(self, hass):
        self.hass = hass

    async def statistics(self, start, end, entities, period, units, kinds):
        try:
            return await get_instance(self.hass).async_add_executor_job(
                statistics_during_period, self.hass, start, end, entities,
                period, units, kinds,
            )
        except HomeAssistantError as error:
            raise HouseholdReadError(str(error)) from error

    async def states(self, start, end, entities, *, with_attributes):
        try:
            history = await get_instance(self.hass).async_add_executor_job(
                lambda: get_significant_states(
                    self.hass, start, end, entities,
                    include_start_time_state=True, significant_changes_only=False,
                    minimal_response=False, no_attributes=not with_attributes,
                )
            )
        except HomeAssistantError as error:
            raise HouseholdReadError(str(error)) from error
        result = {}
        for entity, states in (history or {}).items():
            rows = []
            for state in states:
                changed = getattr(state, 'last_updated', None) or getattr(state, 'last_changed', None)
                if changed is not None:
                    rows.append((changed, getattr(state, 'state', None),
                                 dict(getattr(state, 'attributes', None) or {}) if with_attributes else None))
            result[entity] = rows
        return result

    async def hourly_forecast(self, entity):
        try:
            response = await self.hass.services.async_call(
                'weather', 'get_forecasts', {'entity_id': entity, 'type': 'hourly'},
                blocking=True, return_response=True,
            )
        except HomeAssistantError as error:
            raise HouseholdReadError(str(error)) from error
        return ((response or {}).get(entity) or {}).get('forecast') or []

