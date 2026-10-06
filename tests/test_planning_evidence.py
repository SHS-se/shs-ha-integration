"""Planning evidence keeps the measured model while avoiding duplicate history IO."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock

from household_fixture import Rig
from shs_core.optimisation import build_base_load_model


class PlanningEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_energy_read_preserves_profiles_and_device_membership(self):
        end = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
        start = end - timedelta(days=10)
        devices = [
            {'key': 'pool', 'statistic_id': 'sensor.pool'},
            {'key': 'duplicate-meter', 'statistic_id': 'sensor.pool'},
            {'key': 'private', 'statistic_id': 'sensor.private'},
        ]
        meters = {'sensor.grid': .3, 'sensor.solar': .2, 'sensor.export': .01,
                  'sensor.charge': .05, 'sensor.discharge': .1, 'sensor.pool': .08,
                  'sensor.total_a': .3, 'sensor.total_b': .24, 'sensor.private': .05}
        series = {entity: [{'start': (start + timedelta(minutes=5 * index)).timestamp(),
                            'change': energy + (index % 12) * .001}
                           for index in range(10 * 24 * 12 + 1)]
                  for entity, energy in meters.items()}
        # Intersection and quarter completeness must survive the shared query.
        series['sensor.total_b'].pop(5)
        series['sensor.pool'].pop(8)
        series['sensor.charge'][12]['change'] = -1
        for direct_total in (False, True):
            for excluded_battery in (False, True):
                with self.subTest(direct_total=direct_total, excluded_battery=excluded_battery):
                    options = {'planning_mode': 'live', 'excluded_device_readings':
                               ['private'] + (['$battery'] if excluded_battery else [])}
                    rig = Rig(now=end, options=options)
                    h = rig.household
                    categories = {'grid_import': ['sensor.grid'], 'grid_export': ['sensor.export'],
                                  'solar_production': ['sensor.solar'], 'battery_charge': ['sensor.charge'],
                                  'battery_discharge': ['sensor.discharge'], 'pool_heating': ['sensor.pool']}
                    if direct_total:
                        categories['total_consumption'] = ['sensor.total_a', 'sensor.total_b']
                    async def statistics(query_start, query_end, entities, period, units, kinds):
                        self.assertEqual((query_start, query_end, period, units, kinds),
                                         (start, end, '5minute', {'energy': 'kWh'}, {'change'}))
                        return {entity: series[entity] for entity in entities}
                    rig.history.statistics.side_effect = statistics
                    h._measured_soc_quarters = AsyncMock(return_value={} if excluded_battery else {
                        start.isoformat(): {'battery_soc': .7, 'ev_soc': .4}})
                    previous = await h._actual_quarters(categories, start, end)
                    previous_devices = await h._device_actual_quarters(devices, start, end)
                    expected = build_base_load_model(previous, 'Europe/Stockholm',
                        device_slots=previous_devices, modelled_device_keys=('pool',), now=end)
                    rig.history.statistics.reset_mock()
                    h._measured_soc_quarters.reset_mock()

                    actual, actual_devices = await h._planning_actual_quarters(categories, devices, start, end)

                    rig.history.statistics.assert_awaited_once()
                    h._measured_soc_quarters.assert_not_awaited()
                    self.assertEqual(rig.history.statistics.await_args.args[2],
                                     {entity for ids in categories.values() for entity in ids})
                    self.assertEqual(actual, [{key: value for key, value in row.items()
                        if key not in ('battery_soc', 'ev_soc')} for row in previous])
                    self.assertEqual(actual_devices, previous_devices)
                    self.assertEqual(build_base_load_model(actual, 'Europe/Stockholm',
                        device_slots=actual_devices, modelled_device_keys=('pool',), now=end), expected)

    async def test_upload_still_attaches_soc_to_measured_energy(self):
        start = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
        rig = Rig(now=start + timedelta(minutes=15))
        h = rig.household
        rig.history.statistics.return_value = {'grid': [
            {'start': (start + timedelta(minutes=offset)).timestamp(), 'change': .1}
            for offset in (0, 5, 10)]}
        h._measured_soc_quarters = AsyncMock(return_value={start.isoformat(): {'battery_soc': .7}})
        actual = await h._actual_quarters({'grid_import': ['grid']}, start, rig.now)
        self.assertEqual(actual[0]['battery_soc'], .7)
        self.assertEqual(actual[0]['total_load_kwh'], .3)
        h._measured_soc_quarters.assert_awaited_once_with(start, rig.now)
