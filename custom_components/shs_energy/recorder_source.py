"""Read-only Home Assistant history and forecast adapters."""
import logging
from time import perf_counter, thread_time
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.exceptions import HomeAssistantError
from .shs_core.household_ports import HouseholdReadError

_LOGGER = logging.getLogger(__name__)

class RecorderSource:
    """Read source rows; aggregation and planning belong to the household."""

    def __init__(self, hass):
        self.hass = hass

    async def statistics(self, start, end, entities, period, units, kinds):
        requested = perf_counter()
        def read():
            started, cpu = perf_counter(), thread_time()
            rows = statistics_during_period(self.hass, start, end, entities, period, units, kinds)
            _LOGGER.debug('Recorder statistics: period=%s requested_ids=%s returned_ids=%s rows=%s '
                         'executor_wait_ms=%.1f read_wall_ms=%.1f read_cpu_ms=%.1f',
                         period, len(entities), len(rows), sum(len(values) for values in rows.values()),
                         (started-requested)*1000, (perf_counter()-started)*1000, (thread_time()-cpu)*1000)
            return rows
        try:
            return await get_instance(self.hass).async_add_executor_job(read)
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
