"""Execute the household domain through source, storage and projection ports."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from household_fixture import Rig, cloud_status
from shs_core.api import ShsApiError
from shs_core.household_ports import ConfigurationChanged, HouseholdReadError, HouseholdRefreshError
from shs_core.device_controls import BatteryMeasurementConfigurationError


class HouseholdTests(unittest.IsolatedAsyncioTestCase):
    async def test_dormant_construction_restore_and_first_status_failure_have_no_jobs(self):
        rig = Rig(stored={'optimisation_plan': {'plan_id': 'retained'}})
        h = rig.household
        self.assertFalse(hasattr(h, 'hass'))
        self.assertFalse(hasattr(h, 'entry'))
        rig.client.status.assert_not_awaited()
        await h.async_restore_plan()
        self.assertEqual(h.optimisation_plan, {'plan_id': 'retained'})
        self.assertEqual(rig.spawned, [])
        self.assertEqual(rig.published, [])
        rig.client.status.side_effect = ShsApiError('offline')
        with self.assertRaisesRegex(HouseholdRefreshError, 'offline'):
            await h.async_request_refresh()
        self.assertEqual(h.optimisation_plan, {'plan_id': 'retained'})
        self.assertEqual(rig.records.saved, {'optimisation_plan': {'plan_id': 'retained'}})

    async def test_status_and_repairs_publish_once_and_cached_status_survives_outage(self):
        rig = Rig()
        h = rig.household
        controls, battery = [], []
        h.async_add_control_listener(lambda: controls.append(True))
        h.async_add_battery_listener(lambda: battery.append(True))
        await h.async_request_refresh()
        self.assertEqual(len(rig.published), 1)
        self.assertEqual(controls, [True])
        self.assertEqual(battery, [])
        self.assertTrue(any(item['key'] == 'subscription_inactive' for item in h.attention_items))
        previous = h.data
        rig.client.status.side_effect = ShsApiError('offline')
        await h.async_request_refresh()
        self.assertIs(h.data, previous)
        self.assertEqual(h.last_connection_error, 'offline')
        h._sync_subscription_issue(True)
        self.assertFalse(h.attention_items)
        self.assertEqual(rig.repairs[-1], ('subscription_inactive', None, {}))

    async def test_energy_statistics_stay_utc_half_open_and_use_existing_change_rules(self):
        rig = Rig()
        start = rig.now
        end = start + timedelta(hours=1)
        rig.history.statistics.return_value = {'sensor.energy': [
            {'start': start.timestamp(), 'change': 1.2},
            {'start': start + timedelta(minutes=15), 'change': -4},
            {'start': end, 'change': 99},
            {'start': start + timedelta(minutes=30), 'change': None},
        ]}
        self.assertEqual(await rig.household._statistics_changes(['sensor.energy'], start, end, 'hour'),
                         {'sensor.energy': [(start, 1.2)]})
        rig.history.statistics.assert_awaited_once_with(start, end, {'sensor.energy'}, 'hour', {'energy': 'kWh'}, {'change'})
        self.assertEqual(await rig.household._statistics_changes([], start, end, 'hour'), {})
        self.assertEqual(rig.history.statistics.await_count, 1)

    async def test_local_midnight_tracks_dst_and_daily_groups_use_home_timezone(self):
        for day, expected_hours in ((datetime(2026, 3, 29, 10, tzinfo=timezone.utc), 23),
                                    (datetime(2026, 10, 25, 10, tzinfo=timezone.utc), 25)):
            rig = Rig(now=day)
            start = rig.household.local_midnight()
            end = start + timedelta(days=1)
            self.assertEqual((end.astimezone(timezone.utc)-start.astimezone(timezone.utc)).total_seconds()/3600, expected_hours)
            rig.history.statistics.return_value = {'sensor.energy': [{'start': start.timestamp(), 'change': 2}]}
            values = await rig.household._daily_changes(['sensor.energy'], start, end)
            self.assertEqual(values, {day.date().isoformat(): {'sensor.energy': 2}})

    async def test_weather_is_read_via_forecast_port_and_resampled_without_effects(self):
        rig = Rig()
        horizon = [rig.now, rig.now+timedelta(minutes=15)]
        rig.history.hourly_forecast.return_value = [
            {'datetime': rig.now.isoformat(), 'temperature': 10},
            {'datetime': (rig.now+timedelta(hours=1)).isoformat(), 'temperature': 14},
        ]
        values, source = await rig.household._outdoor_forecast({'weather_forecast_entity': 'weather.home'}, horizon)
        self.assertEqual(source, 'weather.home')
        self.assertEqual(values, dict(zip(horizon, [10, 11])))
        rig.history.hourly_forecast.side_effect = HouseholdReadError('source unavailable')
        self.assertEqual(await rig.household._outdoor_forecast({'weather_forecast_entity': 'weather.home'}, horizon), ({}, None))
        self.assertFalse(rig.published)

    async def test_missing_battery_measurements_enumerate_corrections_before_history_io(self):
        rig = Rig()
        with self.assertRaises(BatteryMeasurementConfigurationError) as error:
            await rig.household.async_battery_loss_statistics({})
        self.assertEqual(len(error.exception.fix['fields']), 4)
        rig.history.statistics.assert_not_awaited()

    async def test_admission_is_awaited_before_configuration_becomes_acknowledged(self):
        rig = Rig(options={'device_modes': {'$battery': 'controlling'},
                           'mode_admissions': {'$battery': 'obsolete'}})
        h = rig.household
        # Excluding a previously admitted battery revokes that admission.
        response = {'device_configuration': [], 'home_configuration': {'battery': {'included': False}}}
        entered, release = asyncio.Event(), asyncio.Event()
        original_admit = h.ports.admit
        async def paused_admit(expected, updated):
            entered.set()
            await release.wait()
            await original_admit(expected, updated)
        h.ports = replace(h.ports, admit=paused_admit)
        stored = {}
        task = asyncio.create_task(h._record_device_exchange(stored, [], response))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertNotIn('optimisation_device_configuration', stored)
            rig.options['rooms'] = {'new': {}}
            release.set()
            with self.assertRaises(ConfigurationChanged):
                await task
            self.assertNotIn('optimisation_device_configuration', stored)
            self.assertEqual(rig.options['rooms'], {'new': {}})
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_real_household_reopens_persisted_battery_runtime_through_observation_port(self):
        from copy import deepcopy
        from test_battery_runtime import Rig as BatteryRig
        source = BatteryRig('control_verification')
        await source.start()
        await source.advance()
        await source.runtime.close()
        restored = BatteryRig('control_verification')
        restored.now = source.now
        restored.rows = deepcopy(source.rows)
        restored.runtime.store = source.store
        household = Rig(options=restored.options).household
        household.ports = replace(household.ports, battery_report=lambda entity: deepcopy(restored.rows.get(entity)))
        household.battery_writer = restored.fence
        household.async_battery_native_readback = restored.coordinator.async_battery_native_readback
        restored.runtime.coordinator = household
        try:
            await restored.fence.open()
            await restored.runtime.open()
            self.assertIsNotNone(restored.runtime.host)
            await asyncio.wait_for(restored.runtime.host.idle(), 2)
            self.assertEqual(restored.runtime.host.state.execution.account.contract.id,
                             source.store.session.account.contract.id)
            self.assertEqual(restored.calls, [])
        finally:
            await restored.runtime.close()
