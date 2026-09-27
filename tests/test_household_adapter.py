"""Exercise HA composition with framework effects replaced only at its edge."""
import ast
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from household_fixture import Rig, Store
from shs_core.durable_record import DurableRecord
from shs_core.household import Household
from shs_core.household_ports import ConfigurationChanged, HomeFacts, HouseholdPorts, HouseholdReadError, HouseholdRefreshError
from shs_core.const import DOMAIN, STORAGE_VERSION, STORAGE_KEY_TEMPLATE


class UpdateFailed(Exception): pass
class HomeAssistantError(Exception): pass


class Base:
    def __class_getitem__(cls, value): return cls
    def __init__(self, hass, logger, **kwargs):
        self.hass = hass
        self.config_entry = kwargs['config_entry']
        self.notifications = 0
    def async_update_listeners(self): self.notifications += 1
    async def async_request_refresh(self):
        self.data = await self._async_update_data()
        self.async_update_listeners()


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = Path(__file__).parents[1]/'custom_components/shs_energy/recorder_source.py'
        tree = ast.parse(path.read_text())
        tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
        self.rig = Rig()
        self.issues = SimpleNamespace(async_create_issue=Mock(), async_delete_issue=Mock(), IssueSeverity=SimpleNamespace(ERROR='error', WARNING='warning'))
        self.history = Mock(return_value={})
        self.statistics = Mock(return_value={})
        async def executor(fn, *args): return fn(*args)
        self.hass = SimpleNamespace(config=SimpleNamespace(latitude=59., longitude=18., language='en'),
            states=SimpleNamespace(get=self.rig.states.get, async_all=lambda: list(self.rig.states.values())),
            config_entries=SimpleNamespace(async_update_entry=lambda entry, **kw: setattr(entry, 'options', kw['options'])),
            services=SimpleNamespace(async_call=AsyncMock(return_value={})))
        self.entry = SimpleNamespace(entry_id='test', options=self.rig.options, async_create_background_task=Mock())
        ns = dict(globals(),
            DataUpdateCoordinator=Base, ir=self.issues, Store=lambda *a: Store(),
            json_bytes=json.dumps, json_loads=json.loads,
            dt_util=SimpleNamespace(utcnow=lambda: self.rig.now, DEFAULT_TIME_ZONE=ZoneInfo('Europe/Stockholm')),
            get_instance=lambda hass: SimpleNamespace(async_add_executor_job=executor),
            get_significant_states=self.history, statistics_during_period=self.statistics,
            refresh_in_progress=lambda *a: False, entity_display_name_by_id=lambda *a: {},
            area_name_by_id=lambda *a: {}, entity_area_id_by_id=lambda *a: {},
            async_energy_dashboard_inventory=AsyncMock(return_value=[]),
        )
        exec(compile(tree, str(path), 'exec'), ns)
        self.source = ns['RecorderSource'](self.hass)

    async def test_history_retains_start_state_and_attributes_only_when_requested(self):
        now = self.rig.now
        self.history.return_value = {'climate.room': [SimpleNamespace(last_updated=now, state='heat', attributes={'hvac_action':'heating'})]}
        for attrs in (True, False):
            result = await self.source.states(now, now, ['climate.room'], with_attributes=attrs)
            self.assertEqual(result['climate.room'], [(now, 'heat', {'hvac_action':'heating'} if attrs else None)])
            self.assertEqual(self.history.call_args.kwargs, dict(include_start_time_state=True, significant_changes_only=False,
                                                              minimal_response=False, no_attributes=not attrs))
        self.history.side_effect = HomeAssistantError('recorder unavailable')
        with self.assertRaises(HouseholdReadError):
            await self.source.states(now, now, ['climate.room'], with_attributes=False)

    async def test_weather_port_uses_only_read_only_forecast_service(self):
        forecast = [{'temperature': 12}]
        self.hass.services.async_call.return_value = {'weather.home': {'forecast': forecast}}
        self.assertEqual(await self.source.hourly_forecast('weather.home'), forecast)
        self.hass.services.async_call.assert_awaited_once_with('weather', 'get_forecasts', {'entity_id':'weather.home','type':'hourly'}, blocking=True, return_response=True)
        self.hass.services.async_call.side_effect = HomeAssistantError('offline')
        with self.assertRaises(HouseholdReadError):
            await self.source.hourly_forecast('weather.home')
