"""Home Assistant composition for the shared household runtime."""
from __future__ import annotations

import logging

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.json import json_bytes
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.json import json_loads

from .configuration import (
    area_name_by_id, async_energy_dashboard_inventory,
    entity_area_id_by_id, entity_display_name_by_id,
)
from .refresh import refresh_in_progress
from .shs_core.const import DOMAIN, STORAGE_VERSION, STORAGE_KEY_TEMPLATE
from .shs_core.durable_record import DurableRecord
from .shs_core.household import Household
from .shs_core.household_ports import (
    ConfigurationChanged, HomeFacts, HouseholdPorts,
    HouseholdReadError, HouseholdRefreshError,
)

_LOGGER = logging.getLogger(__name__)


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


class ShsStatusCoordinator(Household, DataUpdateCoordinator[dict]):
    """Bind the household to HA listeners, stores and canonical configuration."""

    def __init__(self, hass, entry, client):
        DataUpdateCoordinator.__init__(self, hass, _LOGGER, name=f'{DOMAIN}_status', config_entry=entry, update_interval=None)
        self.entry = entry

        async def admit(expected, admitted):
            # This check and update contain no suspension point. A remote adapter
            # must provide the same compare-and-set acknowledgement before returning.
            if dict(entry.options) != expected:
                raise ConfigurationChanged('Home settings changed during admission')
            hass.config_entries.async_update_entry(entry, options=admitted)

        def battery_report(entity):
            state = hass.states.get(entity)
            if state is None:
                return None
            return {'state': state.state, 'attributes': dict(state.attributes),
                    'last_reported': state.last_reported.isoformat(), 'event_id': state.context.id}

        ports = HouseholdPorts(
            home=lambda: HomeFacts(hass.config.latitude, hass.config.longitude, hass.config.language, dt_util.DEFAULT_TIME_ZONE),
            options=lambda: dict(entry.options), admit=admit,
            read_state=hass.states.get,
            entity_ids=lambda: {state.entity_id for state in hass.states.async_all()},
            entity_names=lambda: entity_display_name_by_id(hass),
            area_names=lambda: area_name_by_id(hass),
            entity_areas=lambda: entity_area_id_by_id(hass),
            inventory=lambda: async_energy_dashboard_inventory(hass),
            battery_report=battery_report, history=RecorderSource(hass),
            utcnow=dt_util.utcnow, recovering=lambda: refresh_in_progress(hass, entry),
            repair=self._publish_repair,
            publish=lambda: DataUpdateCoordinator.async_update_listeners(self),
            spawn=lambda coroutine, name: entry.async_create_background_task(hass, coroutine, name=name),
        )
        Household.__init__(self, ports, client,
            store=DurableRecord(Store(hass, STORAGE_VERSION, STORAGE_KEY_TEMPLATE.format(entry_id=entry.entry_id)), json_bytes, json_loads),
            battery_inputs_store=Store(hass, 1, f'shs_energy.battery_live_inputs.{entry.entry_id}'),
        )

    def _publish_repair(self, key, severity, placeholders):
        if severity in (None, 'info'):
            ir.async_delete_issue(self.hass, DOMAIN, key)
            return
        ir.async_create_issue(
            self.hass, DOMAIN, key, is_fixable=False,
            severity=ir.IssueSeverity.ERROR if severity == 'error' else ir.IssueSeverity.WARNING,
            translation_key=key, translation_placeholders=placeholders,
        )

    async def _async_update_data(self):
        try:
            return await Household._async_update_data(self)
        except HouseholdRefreshError as error:
            raise UpdateFailed(str(error)) from error

    async def async_request_refresh(self):
        await DataUpdateCoordinator.async_request_refresh(self)
