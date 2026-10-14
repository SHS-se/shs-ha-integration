from pathlib import Path
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
from shs_core.device_power import read_device_power, planned_switch_bindings, SWITCHED_POWER_MAX_AGE_MS
from shs_core.native_readings import BatteryPowerReadingError


class DevicePowerTests(unittest.TestCase):
    def setUp(self):
        self.now = 10_000_000
        self.reports = {
            'switch.pool': {'state': 'off'},
            'sensor.pool': {'state': '3', 'attributes': {'unit_of_measurement': 'kW', 'state_class': 'measurement'},
                            'last_reported': '1970-01-01T02:30:00+00:00'},
        }

    def read(self, switches=('switch.pool',)):
        return read_device_power('sensor.pool', switches, self.reports.get, now_ms=self.now, max_age_ms=30000)

    def test_off_is_zero_even_if_power_is_old_nonzero_or_unavailable(self):
        for state in ('3', 'unavailable'):
            self.reports['sensor.pool']['state'] = state
            value = self.read()
            self.assertEqual(value.watts, 0)
            self.assertEqual(value.basis, 'switch_off')
            self.assertEqual(value.switch_states, (('switch.pool', 'off'),))
        self.reports.pop('sensor.pool')
        self.assertEqual(self.read().watts, 0)

    def test_on_uses_last_report_for_two_quarters_and_preserves_its_time(self):
        self.reports['switch.pool']['state'] = 'on'
        value = self.read()
        self.assertEqual(value.watts, 3000)
        self.assertEqual(value.reported_at_ms, 9_000_000)
        self.assertEqual(value.at_ms, self.now)
        self.assertEqual(value.basis, 'switched_ac')
        self.now = 9_000_000 + SWITCHED_POWER_MAX_AGE_MS - 1
        self.assertEqual(self.read().valid_until_ms, self.now + 1)
        self.now += 1
        with self.assertRaises(BatteryPowerReadingError):
            self.read()

    def test_switch_state_is_checked_again_when_it_turns_on(self):
        self.now += SWITCHED_POWER_MAX_AGE_MS
        self.assertEqual(self.read().watts, 0)
        self.reports['switch.pool']['state'] = 'on'
        with self.assertRaises(BatteryPowerReadingError):
            self.read()

    def test_unavailable_switch_never_establishes_zero(self):
        for state in ('unavailable', 'unknown'):
            self.reports['switch.pool']['state'] = state
            with self.assertRaisesRegex(BatteryPowerReadingError, 'switch.pool'):
                self.read()
        self.reports.pop('switch.pool')
        with self.assertRaisesRegex(BatteryPowerReadingError, 'switch.pool'):
            self.read()

    def test_all_switches_must_be_off_to_establish_zero(self):
        self.reports['switch.pump'] = {'state': 'on'}
        self.assertEqual(self.read(('switch.pool', 'switch.pump')).watts, 3000)
        self.reports['switch.pump']['state'] = 'off'
        self.assertEqual(self.read(('switch.pool', 'switch.pump')).watts, 0)

    def test_unswitched_measurements_keep_their_existing_reporting_limit(self):
        with self.assertRaises(BatteryPowerReadingError):
            self.read(())

    def test_bindings_use_declared_switches_and_ev_charge_switch_without_exclusions(self):
        devices = [{'key': key, 'planning_role': 'controllable', 'category': category}
                   for key, category in [('pool', 'pool'), ('ev', 'ev_charging'), ('room', 'heating')]]
        options = {'ev_charge_switch_entity': 'switch.ev', 'device_control_mappings': {
            'pool': {'actuator_entity_ids': ['switch.pool']},
            'ev': {'control_entity_id': 'number.current'},
            'room': {'actuator_entity_ids': ['climate.room']},
        }}
        self.assertEqual(planned_switch_bindings(options, devices), {'ev': ('switch.ev',), 'pool': ('switch.pool',)})
        options['excluded_device_readings'] = ['pool']
        self.assertEqual(planned_switch_bindings(options, devices), {'ev': ('switch.ev',)})
