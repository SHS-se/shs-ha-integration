"""A load metered twice must reach its quarter-hour category once."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from household_fixture import Rig
from shs_core.optimisation import category_meters, validate_plan_contract

TOTAL = 'sensor.car_charging_total_energy'
LIFETIME = 'sensor.car_charging_lifetime_energy'
GRID = 'sensor.grid_import'
NOW = datetime(2026, 9, 28, 4, 0, tzinfo=timezone.utc)
CHARGE_START = datetime(2026, 9, 27, 22, 0, tzinfo=timezone.utc)
# The charger's hourly energy on the night of 27-28 September 2026.
CHARGE_KWH = (3.49, 3.51, 3.56, 3.56, 1.47)


def five_minute_changes(hourly, first_hour):
    """Spread each hour's energy evenly over its twelve recorder periods."""
    rows = []
    moment = NOW - timedelta(hours=7)
    while moment < NOW:
        hour = int((moment - first_hour).total_seconds() // 3600)
        energy = hourly[hour] if 0 <= hour < len(hourly) else 0.0
        rows.append({'start': moment.timestamp(), 'change': energy / 12})
        moment += timedelta(minutes=5)
    return rows


class CategoryMeterTests(unittest.TestCase):
    def test_a_category_with_shared_devices_is_measured_by_them_alone(self):
        meters = category_meters(
            {'ev_charging': [LIFETIME], 'grid_import': [GRID], 'hot_water': ['sensor.boiler']},
            [{'statistic_id': TOTAL, 'category': 'ev_charging'},
             {'statistic_id': 'sensor.pool', 'category': 'pool_heating'}],
        )
        self.assertEqual(meters['ev_charging'], [TOTAL])
        self.assertEqual(meters['pool_heating'], ['sensor.pool'])
        # Categories without a shared device keep their configured meters.
        self.assertEqual(meters['grid_import'], [GRID])
        self.assertEqual(meters['hot_water'], ['sensor.boiler'])

    def test_several_devices_in_one_category_are_each_counted_once(self):
        meters = category_meters({}, [
            {'statistic_id': 'sensor.a', 'category': 'heating'},
            {'statistic_id': 'sensor.b', 'category': 'heating'},
            {'statistic_id': 'sensor.a', 'category': 'heating'},
        ])
        self.assertEqual(meters, {'heating': ['sensor.a', 'sensor.b']})

    def test_configured_lists_are_not_mutated(self):
        configured = {'ev_charging': [LIFETIME]}
        category_meters(configured, [{'statistic_id': TOTAL, 'category': 'ev_charging'}])
        self.assertEqual(configured, {'ev_charging': [LIFETIME]})


class ExchangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_old_charger_meter_left_in_the_options_does_not_double_the_charge(self):
        rig = Rig(now=NOW, options={'planning_mode': 'live'})
        h = rig.household
        h._plan_configuration_changed = False
        h._plan_contract = validate_plan_contract
        h.optimisation_missing_inputs = []
        h.last_optimisation_error = None
        h.supplier_prices = []
        # The options still name the Easee lifetime meter, which reports each
        # hour an hour late; the dashboard's charger device is the newer meter.
        h._configured_entities = lambda: {'grid_import': [GRID], 'ev_charging': [LIFETIME]}
        device = {'key': TOTAL, 'statistic_id': TOTAL, 'category': 'ev_charging'}
        h._prepared_device_inventory = AsyncMock(return_value=[device])
        h._observe_calibration = Mock()
        h._thermal_quarters = AsyncMock(return_value=[])
        h._optimisation_options = lambda: rig.options
        h._build_optimisation_snapshot = AsyncMock(return_value={})
        h._price_quarters = lambda *args: []
        h.equipment_presence = lambda: {}
        h._record_device_exchange = AsyncMock(return_value={})
        h._retry_pending_plan_ack = AsyncMock(return_value=False)
        h._sync_plan_refused_issue = Mock()
        h._sync_optimisation_issue = Mock()
        h.async_update_listeners = Mock()
        h.async_report_runtime = AsyncMock()
        h.async_battery_inputs_refresh = AsyncMock()
        h.client = SimpleNamespace(push_optimisation=AsyncMock(return_value={}))
        series = {
            TOTAL: five_minute_changes(CHARGE_KWH, CHARGE_START),
            LIFETIME: five_minute_changes(CHARGE_KWH, CHARGE_START + timedelta(hours=1)),
            GRID: five_minute_changes((4.0,) * 5, CHARGE_START),
        }

        async def statistics(start, end, entities, period, units, kinds):
            if 'change' not in kinds:
                return {}
            return {entity: series[entity] for entity in entities if entity in series}
        rig.history.statistics.side_effect = statistics

        await h.async_optimisation_push(force_plan=True)

        actuals = h.client.push_optimisation.await_args.args[0]
        hourly = {}
        for row in actuals:
            hour = datetime.fromisoformat(row['start']).replace(minute=0)
            hourly[hour] = hourly.get(hour, 0.0) + row['ev_charging_kwh']
            self.assertAlmostEqual(row['ev_charging_kwh'], row['device_energy_kwh'][TOTAL], places=6)
        self.assertEqual(
            [round(hourly.get(CHARGE_START + timedelta(hours=index), 0.0), 2) for index in range(6)],
            [3.49, 3.51, 3.56, 3.56, 1.47, 0.0],
        )
        self.assertAlmostEqual(sum(hourly.values()), sum(CHARGE_KWH), places=4)


if __name__ == '__main__':
    unittest.main()
