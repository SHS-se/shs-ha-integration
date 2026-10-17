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
    async def test_midquarter_replan_subtracts_live_counter_delivery_after_completed_quarters(self):
        from shs_core.planning import build_services
        rig = Rig()
        captured = datetime(2026, 8, 15, 12, 10, tzinfo=timezone.utc)
        start = captured.replace(minute=0)
        rig.states["sensor.boiler_energy"] = SimpleNamespace(state="100.5", attributes={"unit_of_measurement": "kWh"})
        rig.history.statistics.return_value = {"sensor.boiler_energy": [{"start": start - timedelta(minutes=5), "state": 100}]}
        model = {"key": "boiler", "statistic_id": "sensor.boiler_energy", "category": "hot_water",
            "control_type": "permit_inhibit", "planning_role": "controllable", "profile_sample_count": 96,
            "active_power_w": 3000, "forecast_w_by_slot": [0] * 4,
            "inference": {"daily_energy_kwh_by_weekday": {day: 5 for day in range(7)}}}
        partial = await rig.household._boiler_delivered_current_quarter([model], captured)
        self.assertEqual(partial[0]["device_energy_kwh"], {"boiler": .5})
        actuals = [{"start": start.replace(hour=1).isoformat(), "device_energy_kwh": {"boiler": 3}}, *partial]
        services, _, _ = build_services({"device_control_mappings": {"boiler": {
            "control_type": "permit_inhibit", "actuator_entity_ids": ["switch.boiler"]}}},
            [start + timedelta(minutes=15 * i) for i in range(4)], [model],
            read_entity=lambda _: None, local_tz=timezone.utc, device_actuals=actuals)
        self.assertEqual(services[0]["required_kwh"], 1.5)

    async def test_boiler_rating_uses_median_recorder_peaks_in_watts(self):
        rig = Rig()
        rig.history.statistics.return_value = {"sensor.boiler_power": [
            {"max": value} for value in [0, 200, 3040, 3060, 3080, 4500, None]]}
        device = {"key": "boiler", "category": "hot_water", "control_type": "permit_inhibit"}
        result = await rig.household._boiler_running_power_w([device],
            {"boiler": {"power": "sensor.boiler_power"}}, rig.now - timedelta(days=10), rig.now)
        self.assertEqual(result, {"boiler": 3070})
        self.assertEqual(rig.history.statistics.call_args.args[3:], ("hour", {"power": "W"}, {"max"}))

    async def test_cold_start_keeps_unrepresented_running_device_demand_in_residual(self):
        rig = Rig(options={"house_consumption_power_entity": "sensor.house"})
        rig.states["sensor.house"] = SimpleNamespace(state="3500", attributes={"unit_of_measurement": "W"})
        rig.household._mapped_power_w = lambda _: 3000
        model = {"key": "boiler", "active_power_w": 3000, "profile_sample_count": 0,
                 "forecast_w_by_slot": [0] * 96}
        mapping = {"boiler": {"power": "sensor.boiler_power"}}
        self.assertEqual(rig.household._current_base_load_w([model], mapping), 3500)
        model["forecast_w_by_slot"] = [500] * 96
        self.assertEqual(rig.household._current_base_load_w([model], mapping), 3000)


    async def test_missing_boiler_power_attention_targets_its_sensor_editor(self):
        rig = Rig()
        h = rig.household
        h.optimisation_degraded_devices = [{"key": "boiler", "statistic_id": "sensor.boiler_energy",
            "name": "Water boiler", "reason": "needs a measured heating cycle",
            "status": "quiet"}]
        h._sync_degraded_group("boiler_power", "quiet", "Power", "Check boiler power sensor")
        field = h.attention_items[0]["fix"]["fields"][0]
        self.assertEqual(field["key"], "power")
        self.assertEqual(field["device_key"], "sensor.boiler_energy")

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
        self.assertEqual([item['key'] for item in h.attention_items], ['pipeline:cloud_status'])
        self.assertEqual(rig.repairs[-1], ('subscription_inactive', None, {}))
        rig.client.status.side_effect = None
        await h.async_request_refresh()
        self.assertFalse(any(item['key']=='pipeline:cloud_status' for item in h.attention_items))

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
